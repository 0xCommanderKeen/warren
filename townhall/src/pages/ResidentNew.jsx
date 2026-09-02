/* Declaring a resident: the console's nursery form, ported (warren#225).
 *
 * `POST /residents` writes `residents/<id>/manifest.yaml` and `soul.md`, reads them straight
 * back through the ordinary validator, and — since steward#214 — commits them. Ticking
 * deploy hands the same declaration to the nursery, which packs a compose bundle, pipes it
 * over ssh to the host the manifest names, brings the container up and checks the schedule.
 *
 * Two rules carried over from the console and worth keeping:
 *
 * **Validate exactly what the server validates, and send nothing else.** The id pattern and
 * the accent pattern here are the manifest schema's own. A form that sent a body it knew
 * steward would refuse would be spending a round trip to be told something it already knew.
 *
 * **Say what the answer said.** The panel below a successful declaration prints the
 * nursery's own report — which files were written, whether the bundle was sent, which
 * commands ran, what the schedule check found — rather than describing what a deploy
 * usually does. And the ticket beside it stays *accepted* until the new resident comes back
 * through `GET /residents`, because a 201 is steward accepting a declaration, not a
 * resident existing.
 */

import { useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { describeCommit, diagnosticsFor } from "../steward/client.js";
import { confirmDeclared, useLedger } from "../console/ledger.jsx";
import {
  Actions, Badge, Button, Check, Empty, Facts, Field, Input, Loading, Note, Panel, Problem,
  Receipt, Rule, Select, Textarea, Verbatim, buttonClass,
} from "../console/ui.jsx";

const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
const ACCENT_PATTERN = /^#[0-9a-fA-F]{6}$/;
const RUNNERS = ["claude", "codex", "command", "mock"];

const BLANK = {
  id: "",
  name: "",
  char: "",
  role: "",
  accent: "#a68a4f",
  kind: "claude",
  model: "",
  agent_id: "",
  summary: "",
  mission: "",
  duties: "",
  rules: "",
  escalation: "",
  soul_body: "",
  voice: "",
  deploy: false,
};

const lines = (value) =>
  String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

/**
 * Check the draft the way steward's validator will, and say which field is wrong.
 *
 * Returns diagnostics in steward#214's own `{field, problem, example}` shape, so a local
 * complaint and a server refusal land on the same box through the same code path.
 */
export function complaints(draft) {
  const found = [];
  const complain = (field, problem, example) =>
    found.push({ field, problem, example: example ?? null, severity: "error" });

  if (!ID_PATTERN.test(draft.id.trim())) {
    complain(
      "id",
      "Lowercase letters, digits and hyphens only, starting with a letter or digit — the " +
        "pattern the manifest schema enforces.",
      "^[a-z0-9][a-z0-9-]*$",
    );
  }
  if (!ACCENT_PATTERN.test(draft.accent.trim())) {
    complain("accent", "Six hex digits after a #.", "#4f7ea6");
  }
  if (!draft.name.trim()) complain("name", "Required: the village draws this.");
  if (!draft.char.trim()) complain("char", "Required: the village needs a sprite key.");
  if (!draft.role.trim()) complain("role", "Required: one line, shown under the name.");
  if (!draft.mission.trim()) complain("mission", "Required: one paragraph of purpose.");
  if (!lines(draft.duties).length) complain("duties", "At least one duty, one per line.");
  if (!lines(draft.rules).length) complain("rules", "At least one hard rule, one per line.");
  if (!draft.escalation.trim()) {
    complain("escalation", "Required: when this resident stops and asks.");
  }
  return found;
}

/** The body `POST /residents` takes — and nothing steward did not ask for. */
export function declarationBody(draft, defaults) {
  const value = (key) => draft[key].trim();
  const body = {
    id: value("id"),
    name: value("name"),
    char: value("char"),
    accent: value("accent"),
    role: value("role"),
    charter: {
      mission: value("mission"),
      duties: lines(draft.duties),
      rules: lines(draft.rules),
      escalation: value("escalation"),
    },
    runner: { kind: value("kind") || "claude" },
    // Sent as a boolean either way: `deploy: false` is something this form means to say,
    // and asking a machine to start a container is never a field left out.
    deploy: Boolean(draft.deploy),
  };
  if (value("model")) body.runner.model = value("model");
  if (value("agent_id")) body.agent_id = value("agent_id");
  if (value("summary")) body.summary = value("summary");
  if (value("soul_body")) body.soul_body = value("soul_body");
  if (value("voice")) body.voice = value("voice");
  const granted = (draft.skills || []).filter((name) => !defaults.has(name));
  if (granted.length) body.skills = granted;
  return body;
}

/* -- what steward answered ------------------------------------------------------------ */

function ProvisionBlock({ provision }) {
  const target = provision.target || {};
  return (
    <>
      <div className="mb-1.5 mt-5 text-[9.5px] uppercase tracking-[.2em] text-faint">provision</div>
      <Facts
        pairs={[
          ["host", `${target.user}@${target.host}:${target.path}`],
          ["container", target.container],
          ["image", target.image],
          [
            "sent",
            provision.sent
              ? "yes — the bundle was uploaded"
              : "no — the host already matched, byte for byte",
          ],
          [
            "compose",
            provision.compose_changed === null
              ? "not compared: a dry run does not reach the host"
              : provision.compose_changed
                ? "re-rendered"
                : "unchanged",
          ],
          ["files", (provision.files || []).join(", ")],
          [".env carries", `${(provision.env_keys || []).join(", ")} (names only, never values)`],
        ]}
      />
      <div className="mb-1.5 mt-4 text-[9.5px] uppercase tracking-[.2em] text-faint">
        commands steward ran
      </div>
      <ul className="my-0 list-none space-y-1 p-0">
        {(provision.commands || []).map((line) => (
          <li key={line} className="text-[11.5px] text-dim [overflow-wrap:anywhere]">
            {line}
          </li>
        ))}
      </ul>
      <Verbatim value={provision.compose || ""} summary="the compose fragment, verbatim" />
    </>
  );
}

function RegisterBlock({ register }) {
  const fires = register.next_fires || [];
  return (
    <>
      <div className="mb-1.5 mt-5 text-[9.5px] uppercase tracking-[.2em] text-faint">register</div>
      {register.ok ? null : (
        <Problem
          title="the schedule check did not pass"
          error={{ message: (register.problems || []).join("; ") }}
        />
      )}
      {fires.length ? (
        <ul className="my-0 list-none space-y-1 p-0">
          {fires.map((fire) => (
            <li key={fire.routine} className="text-[12px] text-dim">
              {fire.routine} fires next at <code className="text-ink">{fire.at}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-0 mt-1.5 text-[11.5px] leading-[1.6] text-faint">
          No enabled routine, so this resident fires nothing on a schedule. There is no second
          registry: a routine is scheduled because a manifest declares it.
        </p>
      )}
    </>
  );
}

/** The 201, rendered. Every line is a field steward sent back. */
function Declared({ answer, onDismiss }) {
  const commit = describeCommit(answer.commit);
  return (
    <Receipt
      title={answer.provision ? "raised" : "declared — and that is all"}
      status={answer.status}
      commit={commit}
      onDismiss={onDismiss}
    >
      <Facts
        pairs={[
          ["id", answer.id],
          ["manifest", <code>{answer.manifest_path}</code>],
          ["soul", <code>{answer.soul_path}</code>],
          ["request", <code className="[overflow-wrap:anywhere]">{answer.request_id}</code>],
          [
            "declare",
            answer.declare
              ? `${answer.declare.written ? "written" : "already there"} · ${answer.declare.note}`
              : null,
          ],
        ]}
      />
      {(answer.warnings || []).map((line, index) => (
        <p key={index} className="mb-0 mt-2 text-wait">
          {typeof line === "string" ? line : line.problem}
        </p>
      ))}
      {answer.provision ? <ProvisionBlock provision={answer.provision} /> : null}
      {answer.register ? <RegisterBlock register={answer.register} /> : null}
      <p className="mb-0 mt-4 leading-[1.75]">
        Both files were read back through <strong className="text-ink">steward validate</strong>{" "}
        before this answer, so what is on disk is something steward accepts.{" "}
        {answer.provision
          ? "The resident appears in the village when it emits its own first event, not because anything here says it exists."
          : "It is not a resident yet — edit the soul body into somebody real, then deploy it."}
      </p>
      <p className="mb-0 mt-3">
        <Link to={routeTo.resident(answer.id)} className={buttonClass("ghost", true)}>
          Open {answer.id}
        </Link>
      </p>
    </Receipt>
  );
}

/* -- the form ------------------------------------------------------------------------- */

export default function ResidentNew() {
  const { client } = useSteward();
  const { raise } = useLedger();
  const library = useStewardQuery((signal) => client.listSkills({ signal }), []);

  const [draft, setDraft] = useState({ ...BLANK, skills: [] });
  const [local, setLocal] = useState([]);
  const [refusal, setRefusal] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [sending, setSending] = useState(false);

  const skills = library.data?.skills || [];
  const defaults = new Set(skills.filter((skill) => skill.default).map((skill) => skill.name));
  const diagnostics = [...local, ...(refusal?.diagnostics || [])];
  const set = (key) => (event) =>
    setDraft((current) => ({ ...current, [key]: event.target.value }));

  const field = (key, label, hint, props = {}) => (
    <Field label={label} hint={hint} problems={diagnosticsFor(diagnostics, key)}>
      <Input
        value={draft[key]}
        onChange={set(key)}
        invalid={diagnosticsFor(diagnostics, key).length > 0}
        {...props}
      />
    </Field>
  );

  const area = (key, label, hint, rows = 4) => (
    <Field label={label} hint={hint} problems={diagnosticsFor(diagnostics, key)}>
      <Textarea
        rows={rows}
        value={draft[key]}
        onChange={set(key)}
        invalid={diagnosticsFor(diagnostics, key).length > 0}
      />
    </Field>
  );

  async function declare(event) {
    event.preventDefault();
    setRefusal(null);
    setAnswer(null);
    const found = complaints(draft);
    setLocal(found);
    if (found.length) return;

    setSending(true);
    const body = declarationBody(draft, defaults);
    try {
      const reply = await client.createResident(body);
      setAnswer(reply);
      raise({
        what: `${body.deploy ? "raise" : "declare"} ${reply.id}`,
        requestId: reply.request_id,
        why: reply.message,
        confirm: confirmDeclared(client, reply),
      });
      setDraft({ ...BLANK, skills: [] });
    } catch (caught) {
      setRefusal(caught);
    } finally {
      setSending(false);
    }
  }

  if (library.loading && !library.data) return <Loading>reading the skills library…</Loading>;

  return (
    <form onSubmit={declare}>
      {answer ? <Declared answer={answer} onDismiss={() => setAnswer(null)} /> : null}
      {refusal ? <Problem error={refusal} /> : null}
      {local.length ? (
        <Problem
          title="not sent"
          error={{
            message:
              "Some fields do not match what steward's validator accepts. They are marked " +
              "below; nothing has been sent.",
          }}
        />
      ) : null}

      <Panel title="The declaration">
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          {field("id", "id", "Directory under residents/. Lowercase letters, digits and hyphens.", {
            placeholder: "note-keeper",
          })}
          {field("name", "name", "What the village calls it. For example, Hob or Quill.", {
            placeholder: "Quill",
          })}
        </div>
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          {field("char", "char", "The village sprite key.", { placeholder: "Scribe" })}
          {field("role", "role", "One line, lowercase. Shown under the name.", {
            placeholder: "note bot",
          })}
        </div>
        <Field
          label="accent"
          hint="Hex, #rrggbb. The village draws the villager in this colour."
          problems={diagnosticsFor(diagnostics, "accent")}
        >
          <span className="flex items-center gap-2.5">
            <span
              className="size-[26px] flex-none border border-rule"
              style={{
                background: ACCENT_PATTERN.test(draft.accent.trim()) ? draft.accent : "transparent",
              }}
            />
            <Input
              value={draft.accent}
              onChange={set("accent")}
              invalid={diagnosticsFor(diagnostics, "accent").length > 0}
            />
          </span>
        </Field>
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          <Field label="runner kind" hint="Which brain every session for this resident launches on.">
            <Select value={draft.kind} onChange={set("kind")}>
              {RUNNERS.map((kind) => (
                <option key={kind} value={kind} className="bg-void">
                  {kind}
                </option>
              ))}
            </Select>
          </Field>
          {field("model", "runner model", "Passed to the CLI. Blank means that runner's default.", {
            placeholder: "claude-opus-5",
          })}
        </div>
        <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          {field(
            "agent_id",
            "agent_id",
            "Village identity, <source>:<name>. Blank and steward derives one.",
            { placeholder: "derived" },
          )}
          {field("summary", "summary", "One line the village can display. Optional.")}
        </div>
      </Panel>

      <Panel title="Charter">
        {area("mission", "mission", "One paragraph of purpose. Injected into every session.", 5)}
        {area("duties", "duties", "Standing responsibilities, one per line. At least one.", 5)}
        {area("rules", "rules", "Hard constraints, one per line. At least one.", 5)}
        {field("escalation", "escalation", "When to stop and ask instead of acting.", {
          placeholder: "Raise needs_human before anything irreversible.",
        })}
      </Panel>

      <Panel title="Soul body">
        {area(
          "soul_body",
          "opening paragraph",
          "Who this resident actually is. Blank and steward writes a skeleton that says out loud it is one.",
          5,
        )}
        {area(
          "voice",
          "## voice",
          "Style guidance only — it changes nothing a resident may do. Blank means no voice section at all, which is a real answer.",
          4,
        )}
      </Panel>

      <Panel title="Skills">
        <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-dim">
          A grant is what this resident holds on top of the library's defaults. Defaults are
          shown but cannot be unticked: every resident holds them.
        </p>
        {skills.length ? (
          skills.map((skill) => (
            <Check
              key={skill.name}
              name={skill.name}
              note={skill.default ? <Badge tone="ember">default</Badge> : null}
              description={skill.description}
              disabled={skill.default}
              checked={skill.default || draft.skills.includes(skill.name)}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  skills: event.target.checked
                    ? [...current.skills, skill.name]
                    : current.skills.filter((name) => name !== skill.name),
                }))
              }
            />
          ))
        ) : (
          <Empty title="The library is empty.">
            Steward found no skills to grant. A resident can still be declared; it will simply
            hold nothing.
          </Empty>
        )}
      </Panel>

      <Panel title="Deploy">
        <Check
          name="deploy after declaring"
          description={
            "Runs the whole nursery: writes the files, packs a compose bundle, pipes it over " +
            "ssh to the host this manifest addresses, brings the container up, and checks the " +
            "schedule. Left unticked, this form declares and stops."
          }
          checked={draft.deploy}
          onChange={(event) =>
            setDraft((current) => ({ ...current, deploy: event.target.checked }))
          }
        />
      </Panel>

      <Rule />
      <Actions>
        <Button tone="primary" type="submit" disabled={sending}>
          {sending ? "asking steward…" : "Declare resident"}
        </Button>
        <Link to={routeTo.residents()} className={buttonClass("ghost")}>
          Back to residents
        </Link>
        <Note>
          Writes two files and commits them.{" "}
          {draft.deploy
            ? "Deploy is ticked, so the nursery also provisions the host this manifest names."
            : "Deploys nothing, schedules nothing, emits nothing."}
        </Note>
      </Actions>
    </form>
  );
}
