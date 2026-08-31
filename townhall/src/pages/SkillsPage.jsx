/* Skills — the library, and the two ways to write one.
 *
 * The console's skills view said out loud that it was read-only and that no HTTP path
 * wrote a skill. steward#214 built that path, so this page is that view plus the editor it
 * apologised for not being.
 *
 * What is not softened: `defaults: true` is a grant to the entire fleet, and steward
 * revalidates every resident against a library holding the candidate before it will accept
 * one. The form says so where the checkbox is, not in a paragraph somewhere else.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigation } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { describeCommit, diagnosticsFor, normalizeDiagnostics } from "../steward/client.js";
import { Gate } from "../console/Gate.jsx";
import {
  Actions, Badge, Badges, Button, Empty, Field, Input, Loading, Note, PageHead, Panel,
  Problem, Receipt, Row, Rows, Section, Tag, Textarea, Who, buttonClass,
} from "../console/ui.jsx";

/* -- the library --------------------------------------------------------------------- */

function Library() {
  const { client } = useSteward();
  const { data, error, loading } = useStewardQuery((signal) => client.listSkills({ signal }), []);
  const residents = useStewardQuery((signal) => client.listResidents({ signal }), []);

  const names = useMemo(() => {
    const map = new Map();
    for (const resident of residents.data?.residents || []) map.set(resident.id, resident.soul);
    return map;
  }, [residents.data]);

  if (loading) return <Loading>reading the library…</Loading>;
  if (error) return <Problem error={error} />;

  const skills = data?.skills || [];

  return (
    <>
      {(data?.errors || []).map((line) => (
        <Problem key={line} title="skill does not parse" error={{ message: line, code: "unparsable" }} />
      ))}

      {skills.length ? (
        <div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(268px,1fr))]">
          {skills.map((skill) => (
            <Panel className="mb-0 flex flex-col" key={skill.name}>
              <Badges className="mb-2.5">
                {skill.default ? <Badge tone="ember">default</Badge> : <Badge>by grant</Badge>}
                <Badge>
                  {skill.holders.length} holder{skill.holders.length === 1 ? "" : "s"}
                </Badge>
              </Badges>
              <Link to={routeTo.skill(skill.name)} className="font-serif text-[20px] text-ink no-underline hover:text-ember">
                {skill.name}
              </Link>
              <p className="mt-2 flex-1 text-[12px] leading-[1.65] text-dim">{skill.description}</p>
              <div className="mt-2">
                {skill.holders.length ? (
                  skill.holders.map((id) => <Tag key={id}>{names.get(id)?.name || id}</Tag>)
                ) : (
                  <span className="text-faint">
                    Nobody holds this. That is a real answer, not an omission.
                  </span>
                )}
              </div>
              <p className="mb-0 mt-2.5 text-[10.5px] text-faint">
                {skill.path || "no path"} · {skill.body_chars} chars
              </p>
              <Actions className="mt-3">
                <Link to={routeTo.skill(skill.name)} className={buttonClass("ghost", true)}>
                  edit
                </Link>
              </Actions>
            </Panel>
          ))}
        </div>
      ) : (
        <Empty title="No library.">
          Steward found no <code>skills/</code> directory beside the residents tree, or nothing
          in it parses. That is not an error — it means no grant is checked and no skill is
          injected. The first skill written here creates the directory, and steward refuses a
          first skill that would invalidate the fleet.
        </Empty>
      )}

      {residents.data?.residents?.length ? (
        <>
          <Section count={residents.data.residents.length}>Who holds what</Section>
          <Rows>
            <Row head columns="1fr 2.4fr">
              <span>resident</span>
              <span>effective skills — defaults marked</span>
            </Row>
            {residents.data.residents.map((resident) => {
              const granted = new Set((resident.skills || []).map((skill) => skill.id));
              return (
                <Row columns="1fr 2.4fr" key={resident.id}>
                  <Who accent={resident.soul.accent} name={resident.soul.name} id={resident.id} />
                  <span>
                    {resident.effective_skills.map((name) => (
                      <Tag key={name} tone={granted.has(name) ? undefined : "default"}>
                        {name}
                      </Tag>
                    ))}
                  </span>
                </Row>
              );
            })}
          </Rows>
          <p className="mt-4 max-w-[78ch] text-[11px] leading-[1.7] text-faint">
            Amber means a library default — held without a grant. Changing what one resident
            holds is a manifest edit, which lives on that resident's page.
          </p>
        </>
      ) : null}
    </>
  );
}

/* -- the editor ---------------------------------------------------------------------- */

const EMPTY_DRAFT = { name: "", description: "", body: "", defaults: false, revision: null };

function SkillEditor({ name }) {
  const { client } = useSteward();
  const { navigate } = useNavigation();
  const creating = !name;

  const loaded = useStewardQuery(
    (signal) => (creating ? Promise.resolve(null) : client.readSkill(name, { signal })),
    [name],
  );

  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [refusal, setRefusal] = useState(null);
  const [receipt, setReceipt] = useState(null);

  useEffect(() => {
    if (creating) {
      setDraft(EMPTY_DRAFT);
      return;
    }
    if (loaded.data) {
      setDraft({
        name: loaded.data.name,
        description: loaded.data.description || "",
        body: loaded.data.body || "",
        defaults: Boolean(loaded.data.defaults),
        revision: loaded.data.revision || null,
      });
    }
  }, [creating, loaded.data]);

  const set = (key) => (event) =>
    setDraft((previous) => ({
      ...previous,
      [key]: event.target.type === "checkbox" ? event.target.checked : event.target.value,
    }));

  const diagnostics = refusal?.diagnostics || [];
  const warnings = receipt ? normalizeDiagnostics(receipt.warnings) : [];

  async function save(event) {
    event.preventDefault();
    setSaving(true);
    setRefusal(null);
    setReceipt(null);
    try {
      const payload = {
        description: draft.description,
        body: draft.body,
        defaults: draft.defaults,
        ...(draft.revision ? { revision: draft.revision } : {}),
      };
      const answer = creating
        ? await client.createSkill({ ...payload, name: draft.name.trim() })
        : await client.updateSkill(name, payload);
      setReceipt(answer);
      // The revision moved on: keep editing against what is now on disk rather than
      // making the next save a stale one.
      setDraft((previous) => ({ ...previous, revision: answer.revision || null }));
      if (creating && answer.name) navigate(routeTo.skill(answer.name));
    } catch (caught) {
      setRefusal(caught);
    } finally {
      setSaving(false);
    }
  }

  if (!creating && loaded.loading) return <Loading>reading the skill…</Loading>;
  if (!creating && loaded.error) return <Problem error={loaded.error} />;

  return (
    <form onSubmit={save}>
      {receipt ? (
        <Receipt
          title={creating ? "skill added" : "skill replaced"}
          status={receipt.status}
          commit={describeCommit(receipt.commit)}
          onDismiss={() => setReceipt(null)}
        >
          {receipt.message}
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
        </Receipt>
      ) : null}

      {refusal ? <Problem error={refusal} /> : null}

      <Panel title="The skill">
        {creating ? (
          <Field
            label="name"
            hint="The slug, and the directory name under skills/. A name that already exists is refused rather than overwritten."
            problems={diagnosticsFor(diagnostics, "name")}
          >
            <Input
              value={draft.name}
              onChange={set("name")}
              placeholder="triage"
              autoComplete="off"
              spellCheck="false"
              invalid={diagnosticsFor(diagnostics, "name").length > 0 || refusal?.code === "skill_exists"}
            />
          </Field>
        ) : (
          <Field label="name" hint="A skill is renamed in the checkout, not here: this endpoint replaces one name's contents.">
            <Input value={draft.name} readOnly disabled />
          </Field>
        )}

        <Field
          label="description"
          hint="One line saying what this skill is for. It is what the library listing shows."
          problems={diagnosticsFor(diagnostics, "description")}
        >
          <Input
            value={draft.description}
            onChange={set("description")}
            placeholder="Sort the inbox before anything else."
            invalid={diagnosticsFor(diagnostics, "description").length > 0}
          />
        </Field>

        <Field
          label="body"
          hint="The instructions themselves — the whole of SKILL.md below the frontmatter."
          problems={diagnosticsFor(diagnostics, "body")}
        >
          <Textarea
            value={draft.body}
            onChange={set("body")}
            rows={18}
            className="font-mono text-[12px] leading-[1.7]"
            invalid={diagnosticsFor(diagnostics, "body").length > 0}
          />
        </Field>

        <label className="flex cursor-pointer items-start gap-2.5 border border-rule-2 bg-deeper px-[11px] py-[9px]">
          <input
            type="checkbox"
            checked={draft.defaults}
            onChange={set("defaults")}
            className="mt-0.5 accent-ember"
          />
          <span>
            <span className="block">give this skill to every resident</span>
            <span className="mt-[3px] block text-[10.5px] leading-[1.5] text-faint">
              <code>defaults: true</code> is the largest blast radius in the API — a default
              skill is held by every resident without any manifest saying so. Steward
              revalidates the whole tree against a library holding this candidate and refuses
              one that would break somebody's grant.
            </span>
          </span>
        </label>
      </Panel>

      <Actions>
        <Button tone="primary" type="submit" disabled={saving}>
          {saving ? "asking steward…" : creating ? "Add skill" : "Replace skill"}
        </Button>
        <Link to={routeTo.skills()} className={buttonClass("ghost")}>
          Back to the library
        </Link>
        <Note>
          {draft.revision
            ? "Sent with the revision this was loaded at, so a second editor who got there first is told rather than overwritten."
            : "New skills carry no revision — there is nothing yet to be stale against."}
        </Note>
      </Actions>
    </form>
  );
}

/* -- the page ------------------------------------------------------------------------ */

export default function SkillsPage({ page, params }) {
  const { locked } = useSteward();

  const heading =
    page === "skillNew"
      ? { title: "New skill", lede: (
          <>
            Writes <strong className="font-normal text-ink">skills/&lt;name&gt;/SKILL.md</strong> into
            the git-tracked library — never into a session's materialized{" "}
            <code>.claude/skills/</code>, which is pruned wholesale at every wake-up. The
            candidate is validated against the whole fleet on a throwaway copy of the tree
            first; a refusal has written nothing and committed nothing.
          </>
        ) }
      : page === "skill"
        ? { title: params.name, lede: (
            <>
              One skill's frontmatter and body, replaced whole. Steward validates the entire
              residents tree against a library holding this version before it writes, and
              commits what it wrote — so what you get back below is the commit, not a promise.
            </>
          ) }
        : { title: "Skills", lede: (
            <>
              The library steward materializes into every session's skills home, and who holds
              each one. Writable since steward#214: a skill is added and replaced here, while
              granting one to a particular resident stays a manifest edit.
            </>
          ) };

  return (
    <>
      <PageHead
        title={heading.title}
        aside={
          page === "skills" ? (
            <Link to={routeTo.skillNew()} className={buttonClass("primary")}>
              New skill
            </Link>
          ) : null
        }
      >
        {heading.lede}
      </PageHead>

      {locked ? <Gate what="The skills library" /> : page === "skills" ? <Library /> : <SkillEditor name={page === "skill" ? params.name : null} />}
    </>
  );
}
