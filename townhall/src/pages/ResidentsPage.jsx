/* Residents — the fleet steward could validate, and the declaration behind each one.
 *
 * The list is the console's, row for row. The editor is what steward#214 made possible:
 * `GET /residents/{id}/declaration` returns what is actually in git — comments, field
 * order and all — and `PUT` takes that shape back, validated whole-tree on a throwaway copy
 * and committed by steward before it answers.
 *
 * Two spellings, and the difference is stated rather than hidden. **Fields** edits the
 * manifest as data, which steward re-serialises, so YAML comments do not survive it; every
 * block no field here knows about round-trips untouched. **YAML** writes the file byte for
 * byte, which is how comments are kept. Neither is more validated than the other.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { describeCommit, diagnosticsFor, normalizeDiagnostics } from "../steward/client.js";
import {
  changed, getIn, linesToList, listToLines, scalarValue, setIn,
} from "../manifest.js";
import { Gate } from "../console/Gate.jsx";
import {
  Actions, Badge, Badges, Button, Empty, Facts, Field, Gauge, Input, Loading, Note, PageHead,
  Panel, Problem, Receipt, Row, Rows, Section, Select, Stack, Textarea, Verbatim, Who,
  buttonClass,
} from "../console/ui.jsx";

const COLUMNS = "1.5fr .85fr .7fr 1.1fr 1.15fr";

/* -- the list ------------------------------------------------------------------------ */

function ResidentList() {
  const { client } = useSteward();
  const { data, error, loading } = useStewardQuery((signal) => client.listResidents({ signal }), []);

  if (loading) return <Loading>reading the fleet…</Loading>;
  if (error) return <Problem error={error} />;

  const residents = data?.residents || [];

  return (
    <>
      {(data?.errors || []).map((line) => (
        <Problem key={line} title="manifest does not validate" error={{ message: line, code: "invalid" }} />
      ))}

      {residents.length ? (
        <Rows className="rise">
          <Row head columns={COLUMNS}>
            <span>resident</span>
            <span>runner</span>
            <span>skills</span>
            <span>budget</span>
            <span>takes work</span>
          </Row>
          {residents.map((resident) => (
            <Row
              key={resident.id}
              columns={COLUMNS}
              accent={resident.soul.accent}
              href={undefined}
              className="hover:bg-ink/[.03]"
            >
              <Link to={routeTo.resident(resident.id)} className="no-underline text-inherit">
                <Who
                  accent={resident.soul.accent}
                  name={resident.soul.name}
                  id={resident.id}
                  role={resident.soul.role}
                  retired={resident.retired}
                />
              </Link>
              <Stack sub={resident.runner.model || "no model named"}>{resident.runner.kind}</Stack>
              <Stack sub={`${resident.skills.length} granted`}>
                {resident.effective_skills.length} effective
              </Stack>
              <Gauge budget={resident.budget} />
              <Badges>
                {resident.retired ? (
                  <span className="text-faint">nothing — retired</span>
                ) : (
                  <>
                    {resident.board?.claim ? <Badge tone="on">board</Badge> : null}
                    {resident.delegation?.send ? <Badge tone="on">delegates</Badge> : null}
                    {!resident.board?.claim && !resident.delegation?.send ? (
                      <span className="text-faint">routines only</span>
                    ) : null}
                  </>
                )}
              </Badges>
            </Row>
          ))}
        </Rows>
      ) : (
        <Empty title="No residents.">
          Steward found no valid manifest under its residents tree. Either nothing is declared
          yet, or the tree it was pointed at is not the tree you think it is
          (<code>--residents</code>, or <code>STEWARD_RESIDENTS</code>). Declaring the first one
          is <code>steward new-resident</code> or <code>POST /residents</code>; this page edits
          what already exists.
        </Empty>
      )}
    </>
  );
}

/* -- the editor ---------------------------------------------------------------------- */

const RUNNERS = ["claude", "codex", "command", "mock"];

function ManifestFields({ draft, edit, diagnostics }) {
  const text = (path, label, hint, props = {}) => (
    <Field label={label} hint={hint} problems={diagnosticsFor(diagnostics, path)} key={path}>
      <Input
        value={scalarValue(getIn(draft, path))}
        onChange={(event) => edit(path, event.target.value || undefined)}
        invalid={diagnosticsFor(diagnostics, path).length > 0}
        {...props}
      />
    </Field>
  );

  const area = (path, label, hint, rows = 4) => (
    <Field label={label} hint={hint} problems={diagnosticsFor(diagnostics, path)} key={path}>
      <Textarea
        rows={rows}
        value={scalarValue(getIn(draft, path))}
        onChange={(event) => edit(path, event.target.value || undefined)}
        invalid={diagnosticsFor(diagnostics, path).length > 0}
      />
    </Field>
  );

  const list = (path, label, hint, rows = 5) => (
    <Field label={label} hint={hint} problems={diagnosticsFor(diagnostics, path)} key={path}>
      <Textarea
        rows={rows}
        value={listToLines(getIn(draft, path))}
        onChange={(event) => {
          const items = linesToList(event.target.value);
          edit(path, items.length ? items : undefined);
        }}
        invalid={diagnosticsFor(diagnostics, path).length > 0}
      />
    </Field>
  );

  const accent = scalarValue(getIn(draft, "soul.accent"));

  return (
    <>
      <Panel title="Identity">
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
          {text("soul.name", "name", "What Chronicle calls it. Hob, Quill, Maren.")}
          {text("soul.char", "char", "The village sprite key.")}
        </div>
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
          {text("soul.role", "role", "One line, lowercase. Shown under the name.")}
          <Field
            label="accent"
            hint="Hex, #rrggbb. Chronicle draws the villager in this colour."
            problems={diagnosticsFor(diagnostics, "soul.accent")}
          >
            <span className="flex items-center gap-2.5">
              <span
                className="size-[26px] flex-none border border-rule"
                style={{ background: /^#[0-9a-fA-F]{6}$/.test(accent) ? accent : "transparent" }}
              />
              <Input
                value={accent}
                onChange={(event) => edit("soul.accent", event.target.value || undefined)}
                invalid={diagnosticsFor(diagnostics, "soul.accent").length > 0}
              />
            </span>
          </Field>
        </div>
        {text("summary", "summary", "One line Chronicle can display. Optional.")}
      </Panel>

      <Panel title="Charter">
        {area("charter.mission", "mission", "One paragraph of purpose. Injected into every session.", 5)}
        {list("charter.duties", "duties", "Standing responsibilities, one per line. At least one.")}
        {list("charter.rules", "rules", "Hard constraints, one per line. At least one.")}
        {list(
          "charter.escalation.when",
          "escalation · when",
          "The moments this resident must stop and ask instead of acting. One per line.",
        )}
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
          {text("charter.escalation.how", "escalation · how", "How it raises. Usually needs_human.")}
        </div>
        {area("charter.escalation.note", "escalation · note", "What to say when it knocks.", 3)}
      </Panel>

      <Panel title="Runner">
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
          <Field
            label="kind"
            hint="Which brain every session for this resident launches on."
            problems={diagnosticsFor(diagnostics, "runner.kind")}
          >
            <Select
              value={scalarValue(getIn(draft, "runner.kind"))}
              onChange={(event) => edit("runner.kind", event.target.value || undefined)}
            >
              {RUNNERS.includes(getIn(draft, "runner.kind")) ? null : (
                <option value={scalarValue(getIn(draft, "runner.kind"))}>
                  {scalarValue(getIn(draft, "runner.kind")) || "—"}
                </option>
              )}
              {RUNNERS.map((kind) => (
                <option key={kind} value={kind} className="bg-void">
                  {kind}
                </option>
              ))}
            </Select>
          </Field>
          {text("runner.model", "model", "Passed to the CLI. Blank means that runner's default.")}
        </div>
      </Panel>
    </>
  );
}

function DeclarationEditor({ id }) {
  const { client } = useSteward();
  const loaded = useStewardQuery((signal) => client.readDeclaration(id, { signal }), [id]);

  const [mode, setMode] = useState("fields");
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [refusal, setRefusal] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [reloaded, setReloaded] = useState(null);

  // Re-reading after a save must not sweep away the answer the person is still reading —
  // the commit sha is the receipt, and a form that clears it on refresh has told them
  // nothing. The receipt is cleared when a different resident is opened, or by its own ×.
  useEffect(() => {
    setReceipt(null);
    setRefusal(null);
    setReloaded(null);
  }, [id]);

  useEffect(() => {
    if (!loaded.data) return;
    setDraft({
      manifest: loaded.data.manifest,
      text: loaded.data.text,
      soul: loaded.data.soul,
      revision: loaded.data.revision,
    });
  }, [loaded.data]);

  const diagnostics = refusal?.diagnostics || [];
  const warnings = useMemo(() => (receipt ? normalizeDiagnostics(receipt.warnings) : []), [receipt]);
  const dirty = Boolean(
    draft &&
      loaded.data &&
      (changed(draft.manifest, loaded.data.manifest) ||
        draft.text !== loaded.data.text ||
        draft.soul !== loaded.data.soul),
  );

  const edit = (path, value) =>
    setDraft((previous) => ({ ...previous, manifest: setIn(previous.manifest, path, value) }));

  async function save(event) {
    event.preventDefault();
    setSaving(true);
    setRefusal(null);
    setReceipt(null);
    try {
      // Exactly one of `manifest` or `text` — giving both is a 422, and rightly so: they
      // are two spellings of one file and steward will not guess which one you meant.
      const answer = await client.writeDeclaration(id, {
        ...(mode === "yaml" ? { text: draft.text } : { manifest: draft.manifest }),
        soul: draft.soul,
        revision: draft.revision,
      });
      setReceipt(answer);
      setReloaded(null);
      loaded.refresh();
    } catch (caught) {
      setRefusal(caught);
    } finally {
      setSaving(false);
    }
  }

  /**
   * Ask the API process to re-read the tree.
   *
   * Not automatic, and not cosmetic. The scheduler *daemon* watches both trees itself and
   * picks this up within a minute, so a saved declaration is live without any of this. What
   * does not pick it up is the API's own long-lived run-now scheduler and board dispatcher,
   * assembled at startup — so until this is called, firing a routine from a control panel
   * would use the manifest that was on disk when the server booted. The button says which
   * process it reaches, because the difference is the whole point of it.
   */
  async function reload() {
    setReloaded({ state: "asking" });
    try {
      const answer = await client.reload();
      setReloaded({ state: "done", answer });
    } catch (caught) {
      setReloaded({ state: "refused", error: caught });
    }
  }

  if (loaded.loading && !draft) return <Loading>reading the declaration…</Loading>;
  if (loaded.error) return <Problem error={loaded.error} />;
  if (!draft) return null;

  const stale = refusal?.code === "stale_revision";

  return (
    <form onSubmit={save}>
      {receipt ? (
        <Receipt
          title="declaration written"
          status={receipt.status}
          commit={describeCommit(receipt.commit)}
          onDismiss={() => setReceipt(null)}
        >
          {receipt.message}
          {receipt.paths?.length ? (
            <div className="mt-2 text-faint">{receipt.paths.join(" · ")}</div>
          ) : null}
          {warnings.length ? (
            <ul className="mt-2 list-none space-y-1 p-0">
              {warnings.map((item, index) => (
                <li key={index} className="text-wait">
                  {item.field ? `${item.field}: ` : ""}
                  {item.problem}
                </li>
              ))}
            </ul>
          ) : null}
          <div className="mt-3">
            <Button tiny onClick={reload} disabled={reloaded?.state === "asking"}>
              {reloaded?.state === "asking" ? "asking…" : "reload steward's own copy"}
            </Button>
            <p className="mb-0 mt-2 text-[10.5px] leading-[1.6] text-faint">
              The scheduler daemon watches the tree itself and picks this up within a minute,
              so it is already live there. This reaches the API's own run-now scheduler and
              board dispatcher, which were assembled when the server booted.
            </p>
            {reloaded?.state === "done" ? (
              <p className="mb-0 mt-2 text-[11px] text-live">
                {reloaded.answer.status}: {reloaded.answer.residents} residents,{" "}
                {reloaded.answer.routines} routines, {(reloaded.answer.skills || []).length} skills.
              </p>
            ) : null}
          </div>
        </Receipt>
      ) : null}
      {reloaded?.state === "refused" ? <Problem error={reloaded.error} /> : null}

      {refusal ? (
        <Problem error={refusal} />
      ) : null}
      {stale ? (
        <p className="mb-4 text-[12px] leading-[1.7] text-wait">
          Somebody changed this file after you loaded it. Re-read it and reapply your edit —
          steward refused rather than letting one of you silently win.{" "}
          <Button tiny onClick={() => loaded.refresh()}>
            re-read
          </Button>
        </p>
      ) : null}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Button tone={mode === "fields" ? "primary" : "ghost"} tiny onClick={() => setMode("fields")}>
          fields
        </Button>
        <Button tone={mode === "yaml" ? "primary" : "ghost"} tiny onClick={() => setMode("yaml")}>
          yaml
        </Button>
        <Note>
          {mode === "fields"
            ? "Sends the manifest as data. Steward re-serialises it, so the comments in this file do not survive — everything else, including blocks with no field here, round-trips untouched."
            : "Sends the YAML byte for byte, which is how the comments are kept."}
        </Note>
      </div>

      {mode === "fields" ? (
        <ManifestFields draft={draft.manifest} edit={edit} diagnostics={diagnostics} />
      ) : (
        <Panel title={`manifest.yaml — written byte for byte`}>
          <Textarea
            rows={30}
            value={draft.text}
            onChange={(event) => setDraft((previous) => ({ ...previous, text: event.target.value }))}
            className="text-[12px] leading-[1.65]"
            invalid={Boolean(diagnostics.length)}
          />
        </Panel>
      )}

      <Panel title={`${loaded.data.soul_file} — the soul document`}>
        <p className="mt-0 mb-3 text-[12px] leading-[1.7] text-dim">
          The manifest and the soul move together because <code>agent_id</code> is in both and
          validation insists they agree. Omitting the soul would leave it untouched; this form
          always sends it, so what you see is what will be on disk.
        </p>
        <Textarea
          rows={16}
          value={draft.soul}
          onChange={(event) => setDraft((previous) => ({ ...previous, soul: event.target.value }))}
          className="text-[12px] leading-[1.65]"
        />
      </Panel>

      <Panel title="What steward is being sent">
        <Facts
          pairs={[
            ["files", loaded.data.paths?.join(" · ")],
            ["revision", <code className="text-dim">{draft.revision}</code>],
            ["spelling", mode === "yaml" ? "text — byte for byte" : "manifest — re-serialised"],
            ["state", dirty ? "edited, not sent" : "identical to what is on disk"],
          ]}
        />
        {mode === "fields" ? <Verbatim value={draft.manifest} summary="the manifest this form would send" /> : null}
      </Panel>

      <Actions>
        <Button tone="primary" type="submit" disabled={saving}>
          {saving ? "asking steward…" : "Write declaration"}
        </Button>
        <Link to={routeTo.residents()} className={buttonClass("ghost")}>
          Back to residents
        </Link>
        <Link to={routeTo.budgets(id)} className={buttonClass("ghost")}>
          Budget
        </Link>
        <Note>
          Validated whole-tree on a copy before anything is written, and committed by steward
          when it passes. A refusal has written nothing and committed nothing.
        </Note>
      </Actions>
    </form>
  );
}

/* -- the page ------------------------------------------------------------------------ */

export default function ResidentsPage({ page, params }) {
  const { locked } = useSteward();
  const editing = page === "resident";

  return (
    <>
      <PageHead title={editing ? params.id : "Residents"}>
        {editing ? (
          <>
            The editable source of one resident — both files, together. Not the projection the
            fleet page draws, but what is actually in git. It is a full replacement rather than
            a patch, because merging a partial edit would mean steward deciding whether a
            missing key meant cleared or untouched.
          </>
        ) : (
          <>
            Everything steward could validate under its residents tree. A manifest that did not
            validate is named below rather than quietly left out — a fleet list that hides a
            broken resident is worse than one that shows nothing.
          </>
        )}
      </PageHead>

      {locked ? (
        <Gate what={editing ? "A resident's declaration" : "The residents tree"} />
      ) : editing ? (
        <DeclarationEditor id={params.id} />
      ) : (
        <>
          <ResidentList />
          <Section>Creating one</Section>
          <p className="max-w-[78ch] text-[12px] leading-[1.7] text-dim">
            This page edits residents that already exist. Declaring a new one — the nursery flow
            behind <code>POST /residents</code> — is warren#225's half of the console migration
            and is not here yet; <code>steward new-resident</code> does it from a terminal in the
            meantime.
          </p>
        </>
      )}
    </>
  );
}
