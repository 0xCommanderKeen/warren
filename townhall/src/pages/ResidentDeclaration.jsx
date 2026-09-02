import { useEffect, useMemo, useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { useStewardWrite } from "../steward/useStewardWrite.js";
import { describeCommit, diagnosticsFor, normalizeDiagnostics } from "../steward/client.js";
import {
  changed, getIn, linesToList, listToLines, scalarValue, setIn,
} from "../manifest.js";
import {
  Actions, Button, Facts, Field, Input, Loading, Note, Panel, Problem, Receipt, Select,
  Textarea, Verbatim, buttonClass,
} from "../console/ui.jsx";

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
          {text("soul.name", "name", "What Chronicle calls it. For example, Hob or Quill.")}
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
export default function ResidentDeclaration({ id }) {
  const { client, declarationRecoveries, setDeclarationRecovery } = useSteward();
  const loaded = useStewardQuery((signal) => client.readDeclaration(id, { signal }), [id]);
  const recovery = declarationRecoveries.get(id) || null;

  const [mode, setMode] = useState("fields");
  const [draft, setDraft] = useState(() => recovery?.draft || null);
  const [reloaded, setReloaded] = useState(null);
  const [copied, setCopied] = useState(null);
  const [recoveryReadRequest, setRecoveryReadRequest] = useState(null);

  const {
    saving, refusal, receipt, save: write, reset: resetWrite,
    clearRefusal, clearReceipt,
  } = useStewardWrite(
    ({ draft: rejected, mode: spelling }) =>
      client.writeDeclaration(id, {
        ...(spelling === "yaml" ? { text: rejected.text } : { manifest: rejected.manifest }),
        soul: rejected.soul,
        revision: rejected.revision,
      }),
    {
      onStale: (_caught, rejected) => setDeclarationRecovery(id, rejected),
    },
  );

  // Re-reading after a save must not sweep away the answer the person is still reading —
  // the commit sha is the receipt, and a form that clears it on refresh has told them
  // nothing. The receipt is cleared when a different resident is opened, or by its own ×.
  useEffect(() => {
    setDraft(recovery?.draft || null);
    if (recovery) setMode(recovery.mode);
    resetWrite();
    setReloaded(null);
    setCopied(null);
    setRecoveryReadRequest(null);
    // Recovery belongs to the resident and intentionally survives route unmounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!loaded.data) return;
    // A stale write makes the editor a two-version workspace. A re-read updates the
    // server side of that workspace; it must never replace the rejected side.
    if (recovery) return;
    setDraft({
      manifest: loaded.data.manifest,
      text: loaded.data.text,
      soul: loaded.data.soul,
      revision: loaded.data.revision,
    });
  }, [loaded.data, recovery]);

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
    // Exactly one of `manifest` or `text` — giving both is a 422, and rightly so: they
    // are two spellings of one file and steward will not guess which one you meant.
    const answer = await write({ draft, mode });
    if (!answer) return;
    setDeclarationRecovery(id, null);
    setReloaded(null);
    loaded.refresh();
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

  if (loaded.loading && !loaded.data && !recovery) return <Loading>reading the declaration…</Loading>;
  if (loaded.error && !loaded.data && !recovery) return <Problem error={loaded.error} />;
  if (!draft) return null;

  const stale = refusal?.code === "stale_revision";
  const currentWasReread = Boolean(
    recovery &&
      loaded.data &&
      recoveryReadRequest !== null &&
      loaded.successfulRequestId === recoveryReadRequest,
  );

  function rereadCurrent() {
    setRecoveryReadRequest(loaded.refresh());
  }

  function reapplyRejected() {
    if (!recovery || !loaded.data) return;
    // The editor remains a working copy after the refusal. Preserve anything the operator
    // changed while comparing; only advance the optimistic-concurrency token.
    setDraft((previous) => ({ ...(previous || recovery.draft), revision: loaded.data.revision }));
    clearRefusal();
  }

  function discardRejected() {
    if (!loaded.data) return;
    setDraft({
      manifest: loaded.data.manifest,
      text: loaded.data.text,
      soul: loaded.data.soul,
      revision: loaded.data.revision,
    });
    setDeclarationRecovery(id, null);
    clearRefusal();
    setCopied(null);
  }

  async function copyRejected(document) {
    const value =
      document === "soul"
        ? recovery.draft.soul
        : recovery.mode === "yaml"
          ? recovery.draft.text
          : JSON.stringify(recovery.draft.manifest, null, 2);
    try {
      if (!globalThis.navigator?.clipboard?.writeText) throw new Error("clipboard unavailable");
      await globalThis.navigator.clipboard.writeText(value);
      setCopied(document);
    } catch {
      setCopied("failed");
    }
  }

  return (
    <form onSubmit={save}>
      {loaded.error ? <Problem error={loaded.error} /> : null}
      {receipt ? (
        <Receipt
          title="declaration written"
          status={receipt.status}
          commit={describeCommit(receipt.commit)}
          onDismiss={clearReceipt}
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
          Somebody changed this declaration after you loaded it. Your rejected manifest and
          soul are held below until you discard or successfully reapply them. Steward refused
          rather than letting one of you silently win.
        </p>
      ) : null}

      {recovery ? (
        <Panel title="Stale draft recovery">
          <p className="mt-0 text-[12px] leading-[1.7] text-wait">
            {currentWasReread
              ? "Current server files are shown beside the complete rejected draft. Reapply keeps the editor as it is, including newer edits, and changes only the revision used by the next write."
              : "Re-read the current server files to compare them. Refresh cannot alter the rejected draft held here."}
          </p>
          <div className="mb-3 flex flex-wrap gap-2">
            {!currentWasReread ? (
              <Button tiny onClick={rereadCurrent}>re-read current server files</Button>
            ) : (
              <Button tiny tone="primary" onClick={reapplyRejected}>reapply rejected draft</Button>
            )}
            <Button tiny onClick={() => copyRejected("manifest")}>
              {copied === "manifest" ? "manifest copied" : "copy rejected manifest"}
            </Button>
            <Button tiny onClick={() => copyRejected("soul")}>
              {copied === "soul" ? "soul copied" : "copy rejected soul"}
            </Button>
            {copied === "failed" ? <Note>clipboard unavailable — select the draft below</Note> : null}
            {currentWasReread ? <Button tiny onClick={discardRejected}>discard rejected draft</Button> : null}
          </div>
          <div className={`grid gap-3 ${currentWasReread ? "lg:grid-cols-2" : ""}`}>
            {currentWasReread ? (
              <div>
                <p className="text-[10px] uppercase tracking-[.16em] text-faint">current manifest</p>
                {recovery.mode === "yaml" ? (
                  <pre className="overflow-x-auto whitespace-pre-wrap border border-rule-2 bg-void p-[11px] text-[11px] text-dim">
                    {loaded.data.text}
                  </pre>
                ) : (
                  <Verbatim value={loaded.data.manifest} summary="manifest now on the server" />
                )}
                <p className="text-[10px] uppercase tracking-[.16em] text-faint">current soul</p>
                <pre className="overflow-x-auto whitespace-pre-wrap border border-rule-2 bg-void p-[11px] text-[11px] text-dim">
                  {loaded.data.soul}
                </pre>
              </div>
            ) : null}
            <div>
              <p className="text-[10px] uppercase tracking-[.16em] text-faint">rejected manifest</p>
              {recovery.mode === "yaml" ? (
                <pre className="overflow-x-auto whitespace-pre-wrap border border-rule-2 bg-void p-[11px] text-[11px] text-dim">
                  {recovery.draft.text}
                </pre>
              ) : (
                <Verbatim value={recovery.draft.manifest} summary="complete rejected manifest" />
              )}
              <p className="text-[10px] uppercase tracking-[.16em] text-faint">rejected soul</p>
              <pre className="overflow-x-auto whitespace-pre-wrap border border-rule-2 bg-void p-[11px] text-[11px] text-dim">
                {recovery.draft.soul}
              </pre>
            </div>
          </div>
        </Panel>
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

      <Panel title={`${loaded.data?.soul_file || "soul.md"} — the soul document`}>
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
            ["files", loaded.data?.paths?.join(" · ") || "current files unavailable"],
            ["revision", <code className="text-dim">{draft.revision}</code>],
            ["spelling", mode === "yaml" ? "text — byte for byte" : "manifest — re-serialised"],
            [
              "state",
              !loaded.data
                ? "current files unavailable"
                : dirty
                  ? "edited, not sent"
                  : "identical to what is on disk",
            ],
          ]}
        />
        {mode === "fields" ? <Verbatim value={draft.manifest} summary="the manifest this form would send" /> : null}
      </Panel>

      <Actions>
        <Button tone="primary" type="submit" disabled={saving}>
          {saving ? "asking steward…" : "Write declaration"}
        </Button>
        <Link to={routeTo.resident(id)} className={buttonClass("ghost")}>
          Back to the record
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
