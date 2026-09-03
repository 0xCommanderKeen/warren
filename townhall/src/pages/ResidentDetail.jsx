/* One resident, whole: who it is, what it is held to, what it is spending, what it wrote,
 * and what is waiting for it.
 *
 * The steward console's resident page, ported (warren#225). Every panel is a read from
 * steward, and that is a finding rather than a default: Chronicle's `/state` — which the
 * Fleet page draws from, unauthenticated — carries journal *metadata* (a day, a routine, a
 * path) but never the text, carries no inbox at all, and carries no budget or spend. So the
 * three panels this page exists for cannot come from the projection, and asking it for them
 * would mean rendering an emptier answer than steward has.
 *
 * The five reads are settled independently rather than awaited together, because a resident
 * whose journal directory is missing should still show its charter. A panel that could not
 * be read renders steward's refusal in its own place, which is a smaller and more useful
 * failure than a page that is entirely a stack trace.
 */

import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import {
  Badge, DetailHead, Empty, Facts, Loading, Panel, Problem, Section, Swatch, Tag, buttonClass,
} from "../console/ui.jsx";
import { ResidentRoutines, SchedulerBadge, SchedulerNote } from "../console/routines.jsx";
import { LifecyclePanel } from "../console/lifecycle.jsx";
import { stamp } from "../console/time.js";
import { agentUuid, payloadSummary } from "../model.js";

/** Read five things at once and keep each answer separate, refusals included. */
function useResidentPanels(id) {
  const { client } = useSteward();
  return useStewardQuery(
    (signal) =>
      Promise.all(
        [
          client.readResident(id, { signal }),
          client.listRoutines({ signal }),
          client.readBudget(id, { signal }),
          client.readJournal(id, { signal, query: { limit: 8 } }),
          client.readInbox(id, { signal }),
        ].map((promise) => promise.then((value) => ({ value }), (error) => ({ error }))),
      ).then(([resident, routines, budget, journal, inbox]) => ({
        resident,
        routines,
        budget,
        journal,
        inbox,
      })),
    [id],
  );
}

/* -- the panels ----------------------------------------------------------------------- */

function SoulPanel({ resident }) {
  const soul = resident.soul;
  return (
    <Panel title="Soul">
      <Facts
        pairs={[
          [
            "name",
            <span className="flex items-baseline gap-2.5">
              <Swatch accent={soul.accent} />
              {soul.name}
            </span>,
          ],
          ["char", soul.char],
          ["role", soul.role],
          ["accent", <code className="text-dim">{soul.accent}</code>],
          ["summary", resident.summary],
          // The durable name. `id` above the fold is what a person calls this resident; the
          // uid is what outlives a retirement and a reuse of that name, and it is what this
          // page's own URL is keyed on — so it belongs where it can be read and copied.
          ["uid", <code className="text-dim [overflow-wrap:anywhere]">{resident.uid}</code>],
          ["manifest", <code className="text-dim [overflow-wrap:anywhere]">{resident.path}</code>],
          [
            "memory",
            <code className="text-dim [overflow-wrap:anywhere]">
              {resident.memory?.kind}: {resident.memory?.path}
            </code>,
          ],
          [
            "runner",
            `${resident.runner.kind}${resident.runner.model ? ` · ${resident.runner.model}` : ""}`,
          ],
        ]}
      />
      <h3 className="mb-[14px] mt-6 font-mono text-[11px] font-normal uppercase leading-none tracking-[.2em] text-dim">
        Voice
      </h3>
      {resident.voice ? (
        <blockquote className="m-0 border-l-2 border-ember/40 pl-4 font-serif text-[15px] leading-[1.75] text-read">
          {resident.voice}
        </blockquote>
      ) : (
        <p className="m-0 text-[12px] leading-[1.7] text-faint">
          This soul declares no <code>## Voice</code> section, so sessions for it get none.
          Steward injects what the file says and never writes a voice on a resident's behalf.
        </p>
      )}
    </Panel>
  );
}

function CharterPanel({ charter }) {
  const escalation = charter.escalation;
  const list = (items) => (
    <ul className="my-1.5 list-none space-y-1.5 p-0">
      {(items || []).map((item, index) => (
        <li key={index} className="border-l border-rule pl-3 text-[12px] leading-[1.65] text-read">
          {item}
        </li>
      ))}
    </ul>
  );
  return (
    <Panel title="Charter">
      <p className="mb-4 mt-0 text-[12.5px] leading-[1.7] text-read">{charter.mission}</p>
      <div className="mb-1 mt-4 text-[9.5px] uppercase tracking-[.2em] text-faint">duties</div>
      {list(charter.duties)}
      <div className="mb-1 mt-4 text-[9.5px] uppercase tracking-[.2em] text-faint">hard rules</div>
      {list(charter.rules)}
      <div className="mb-1 mt-4 text-[9.5px] uppercase tracking-[.2em] text-faint">escalation</div>
      {typeof escalation === "string" ? (
        <p className="mb-0 mt-1.5 text-[12px] leading-[1.65] text-read">{escalation}</p>
      ) : (
        <>
          {list(escalation?.when)}
          <p className="mb-0 mt-1.5 text-[11.5px] text-dim">
            raised as {escalation?.how}
            {escalation?.note ? ` — ${escalation.note}` : ""}
          </p>
        </>
      )}
    </Panel>
  );
}

function SkillsPanel({ resident }) {
  const granted = new Map((resident.skills || []).map((skill) => [skill.id, skill.note]));
  return (
    <Panel title="Effective skills">
      <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-dim">
        What a session for this resident is actually given: the library's defaults plus this
        manifest's own grants, in injection order.
      </p>
      {resident.effective_skills?.length ? (
        <div>
          {resident.effective_skills.map((name) => (
            <Tag
              key={name}
              tone={granted.has(name) ? undefined : "default"}
              title={
                granted.has(name)
                  ? granted.get(name) || "granted by this manifest"
                  : "a library default — every resident holds it without a grant"
              }
            >
              {name}
            </Tag>
          ))}
        </div>
      ) : (
        <p className="m-0 text-[12px] leading-[1.7] text-faint">
          None. Either the library is empty or steward was pointed at no library at all, in
          which case no grant is checked and no skill is injected.
        </p>
      )}
    </Panel>
  );
}

function RoutinesPanel({ resident, settled, client, refresh, scheduler }) {
  const rows = settled.value
    ? (settled.value.routines || []).filter((row) => row.resident === resident.id)
    : [];
  return (
    <Panel title="Routines">
      {settled.error ? (
        <Problem error={settled.error} />
      ) : rows.length ? (
        <>
          <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-dim">
            <SchedulerBadge scheduler={scheduler} />
            <SchedulerNote scheduler={scheduler} />
          </p>
          <ResidentRoutines rows={rows} client={client} onSettled={refresh} />
        </>
      ) : (
        <Empty title="No standing work.">
          This resident declares no routines, so it only ever wakes for work handed to it — a
          job it claims from the board, or something delegated into its inbox.
        </Empty>
      )}
    </Panel>
  );
}

function BudgetPanel({ settled, id }) {
  if (settled.error) {
    return (
      <Panel title="Budget">
        <Problem error={settled.error} />
      </Panel>
    );
  }
  const budget = settled.value;
  const spent = budget.spent || {};
  const caps = budget.budgets || [];
  return (
    <Panel title="Budget">
      <p className="mb-4 mt-0 text-[12px] leading-[1.7] text-dim">
        Counted in {budget.window.tz}, for the {budget.window.day} window that runs to{" "}
        {stamp(budget.window.end)}. Every number is a sum over rows steward wrote when a run
        finished — nothing here is projected.
      </p>
      {budget.paused ? (
        <Problem
          title="paused"
          error={{
            message:
              `${budget.pause.reason}. Scheduled fires and board claims are skipped while ` +
              `this stands. Answer approval ${budget.pause.request_id} to carry on until the ` +
              "window ends.",
          }}
        />
      ) : null}
      {caps.length ? (
        <dl className="m-0 grid grid-cols-[max-content_1fr_1fr_1fr] gap-x-5 gap-y-2 text-[12px]">
          <div className="contents text-[9.5px] uppercase tracking-[.17em] text-faint">
            <span>budget</span>
            <span>spent</span>
            <span>limit</span>
            <span>remaining</span>
          </div>
          {caps.map((item) => (
            <div className="contents" key={item.budget}>
              <span>{item.budget}</span>
              <span className={item.exhausted ? "text-fail" : "text-dim"}>{String(item.spent)}</span>
              <span className="text-dim">
                {item.limit === null ? "no cap declared" : String(item.limit)}
              </span>
              <span className="text-dim">
                {item.remaining === null ? "—" : String(item.remaining)}
              </span>
            </div>
          ))}
        </dl>
      ) : (
        <Empty title="No caps declared.">
          This resident has no budget block, so nothing stops it but its own schedule. That is
          unlimited, not unknown.
        </Empty>
      )}
      <Facts
        className="mt-4"
        pairs={[
          [
            "runs",
            `${spent.runs || 0}${
              spent.unreported_runs ? ` (${spent.unreported_runs} reported no usage)` : ""
            }`,
          ],
          ["tokens", String(spent.tokens || 0)],
          ["cost", `$${(spent.cost_usd || 0).toFixed(4)}`],
          ["seconds", String(Math.round(spent.duration_s || 0))],
          ["max run", budget.max_run_seconds ? `${budget.max_run_seconds}s` : null],
        ]}
      />
      <p className="mb-0 mt-4">
        <Link to={routeTo.budgets(id)} className={buttonClass("ghost", true)}>
          Edit the caps
        </Link>
      </p>
    </Panel>
  );
}

function JournalPanel({ settled }) {
  if (settled.error) {
    return (
      <Panel title="Journal">
        <Problem error={settled.error} />
      </Panel>
    );
  }
  const entries = settled.value.entries || [];
  return (
    <Panel title="Journal">
      {entries.length ? (
        entries.map((entry, index) => (
          <div key={`${entry.date}:${index}`} className="mb-4 border-l border-rule pl-4 last:mb-0">
            <div className="text-[10px] uppercase tracking-[.14em] text-faint">
              {entry.date}
              {entry.routine ? ` · ${entry.routine}` : " · no routine named"}
            </div>
            <p className="mb-0 mt-1.5 whitespace-pre-wrap text-[12.5px] leading-[1.75] text-read">
              {entry.text}
            </p>
          </div>
        ))
      ) : (
        <Empty title="Nothing written yet.">
          The resident writes this, not steward — an entry appears when its closing routine
          runs and the session actually writes one. No entry is invented on a resident's
          behalf, so an empty journal means a day that was never closed.
        </Empty>
      )}
    </Panel>
  );
}

/**
 * One line naming which doors are open and how much post is behind them.
 *
 * A closed route is named as closed rather than omitted: letters delivered before somebody
 * shut it stay open and nothing picks them up, and a panel listing only accepting routes
 * would show no route at all and leave the pile unexplained.
 */
function routeLine(routes, pending) {
  if (!routes.length) {
    return "This resident declares no delegation route, so nothing can be handed to it.";
  }
  const open = routes.filter((route) => route.accepts).map((route) => route.id);
  if (open.length) return `Open routes: ${open.join(", ")}. ${pending} waiting.`;
  const shut = routes.map((route) => `${route.id} (${route.status})`).join(", ");
  return pending
    ? `Every declared route is closed — ${shut}. ${pending} waiting behind it, and nothing ` +
        "will pick them up."
    : `Every declared route is closed — ${shut}. Nothing can be handed to this resident.`;
}

function InboxPanel({ settled }) {
  if (settled.error) {
    return (
      <Panel title="Inbox — delegated and waiting">
        <Problem error={settled.error} />
      </Panel>
    );
  }
  const items = settled.value.inbox || [];
  return (
    <Panel title="Inbox — delegated and waiting">
      <p className="mb-4 mt-0 text-[12px] leading-[1.7] text-dim">
        {routeLine(settled.value.routes || [], settled.value.pending || 0)}
      </p>
      {items.length ? (
        items.map((item) => (
          <div key={item.task_id} className="mb-4 border-l border-rule pl-4 last:mb-0">
            <div className="text-[10px] uppercase tracking-[.14em] text-faint">
              {item.status} · from {item.delegated_by || "a person"} via {item.route} · depth{" "}
              {item.depth}
            </div>
            <div className="mt-1.5 text-[12.5px] leading-[1.65]">{item.title}</div>
            {item.detail ? (
              <div className="mt-1 text-[11.5px] leading-[1.6] text-dim">{item.detail}</div>
            ) : null}
          </div>
        ))
      ) : (
        <Empty title="Empty inbox.">
          Nothing is waiting. Work handed to this resident lands here and is picked up on its
          own next wake-up; steward never prompts a resident to check.
        </Empty>
      )}
    </Panel>
  );
}

function EventFeed({ resident, model }) {
  const person = model?.people.find(
    (item) => item.residency === "resident" && agentUuid(item.id) === resident.uid,
  );
  const events = (person?.history || []).slice().reverse();

  return (
    <>
      <Section>Event feed</Section>
      <p className="mb-5 max-w-[78ch] text-[12px] leading-[1.7] text-dim">
        Chronicle's retained activity for <code>resident:{resident.uid}</code>, newest first.
        This follows the live village snapshot; it is a bounded window, not a permanent log.
      </p>
      {!model ? (
        <Empty title="Waiting for Chronicle.">
          The resident record is available, but the live village snapshot has not arrived yet.
        </Empty>
      ) : !person ? (
        <Empty title="No Chronicle identity found.">
          Chronicle's current snapshot does not contain this resident. It may not have emitted
          an event since the latest log reset.
        </Empty>
      ) : events.length ? (
        <div className="border-t border-rule" aria-label={`${resident.soul.name} event feed`}>
          {events.map((event, index) => {
            const summary = payloadSummary(event) || "No payload summary";
            return (
              <div
                className="grid gap-1.5 border-b border-rule py-3.5 md:grid-cols-[150px_145px_1fr] md:gap-5"
                key={`${event.ts}:${event.type}:${index}`}
              >
                <time className="font-mono text-[10px] text-faint" dateTime={event.ts}>
                  {stamp(event.ts)}
                </time>
                <span className="font-mono text-[10px] uppercase tracking-[.12em] text-ember">
                  {event.type || "unknown"}
                </span>
                <span className="min-w-0 break-words font-mono text-[10.5px] leading-[1.6] text-dim">
                  {summary}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <Empty title="No retained activity.">
          Chronicle knows this resident, but its current snapshot carries no event history.
        </Empty>
      )}
    </>
  );
}

/* -- the page ------------------------------------------------------------------------- */

export default function ResidentDetail({ id, model }) {
  const { client } = useSteward();
  const { data, error, loading, refresh } = useResidentPanels(id);

  if (loading && !data) return <Loading>reading the resident…</Loading>;
  if (error) return <Problem error={error} />;
  if (!data) return null;

  const back = (
    <Link
      to={routeTo.residents()}
      className="text-[11px] uppercase tracking-[.16em] text-ember no-underline"
    >
      ← all residents
    </Link>
  );

  if (data.resident.error) {
    return (
      <>
        <DetailHead title={id} back={back} />
        <Problem error={data.resident.error} />
      </>
    );
  }

  const resident = data.resident.value;
  const scheduler = data.routines.value?.scheduler || null;

  return (
    <>
      <DetailHead
        accent={resident.soul.accent}
        title={
          <>
            {resident.soul.name}
            {resident.retired ? (
              <>
                {" "}
                <Badge tone="fail">retired</Badge>
              </>
            ) : null}
          </>
        }
        back={back}
        aside={
          <>
            <Link to={routeTo.residentDeclaration(id)} className={buttonClass("ghost", true)}>
              Edit declaration
            </Link>
          </>
        }
      >
        {resident.soul.role} · {resident.id}
        {resident.agent_id ? ` · ${resident.agent_id}` : null}
        {resident.project ? ` · project ${resident.project}` : null}
        {resident.retired ? " · retired: takes no routines, no board work, no letters" : null}
      </DetailHead>

      <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
        <SoulPanel resident={resident} />
        <CharterPanel charter={resident.charter} />
      </div>
      <SkillsPanel resident={resident} />
      <RoutinesPanel
        resident={resident}
        settled={data.routines}
        client={client}
        refresh={refresh}
        scheduler={scheduler}
      />
      <LifecyclePanel resident={resident} refresh={refresh} />
      <BudgetPanel settled={data.budget} id={id} />
      <JournalPanel settled={data.journal} />
      <InboxPanel settled={data.inbox} />

      <EventFeed resident={resident} model={model} />

      <Section>Where the rest of this resident is</Section>
      <p className="max-w-[78ch] text-[12px] leading-[1.7] text-dim">
        The village's view of the same resident — what it has been <em>doing</em> — is the{" "}
        <Link to={routeTo.fleet()} className="text-ember no-underline">
          Fleet
        </Link>{" "}
        page. The event feed above is Chronicle's; the declaration, journal text, inbox, and
        spend on this page are steward's and need an operator credential.
      </p>
    </>
  );
}

export { routeLine };
