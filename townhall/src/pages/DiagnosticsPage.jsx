/* Everything the projection could not fold cleanly, and every knock nobody answered.
 *
 * Read-only, and Chronicle's alone: this page never touches steward, so it needs no
 * credential — the same deal the fleet page has. It draws the snapshot's `diagnostics`,
 * which the transport has always carried here and nothing had ever rendered (warren#279).
 *
 * The chat drop is why this page exists rather than a `curl`. warren#108 asked for a knock
 * at a resident's bot to be "visible in the village"; warren#276 made every drop a bounded
 * record naming the door, who knocked and why they got silence. Until this page there was
 * nowhere in the fleet's own surfaces to see one.
 *
 * Two rules, both enforced in `../diagnostics.js` rather than here:
 *   - a knock storm is one line with a count, because it is one fact;
 *   - a knock renders six named fields and nothing else, so there is nowhere for a
 *     stranger's message to land. The event carries none by design (steward/docs/chat.md),
 *     and this panel must never be the thing that gives a chat bot a way to publish text
 *     into the operator's own screen. Every *other* kind is drawn from whatever fields it
 *     carries — that is what makes an unfamiliar kind visible at all — and what keeps that
 *     safe is Chronicle's rule rather than this file's: a diagnostic names what went wrong
 *     without quoting the input that caused it.
 *
 * Arcadia is deliberately not the place for this: the village scene shows what villagers are
 * *doing*, and a knock is precisely not the villager doing anything.
 */

import { Badge, Clock, Empty, Facts, Loading, PageHead, Row, Rows, Section, Stack } from "../console/ui.jsx";
import { instant, span } from "../console/time.js";
import { KIND_WORDS, KNOCK, UNNAMED, fields, groupDiagnostics, plural } from "../diagnostics.js";

const KNOCK_COLUMNS = "1.1fr 1.1fr .9fr 1fr auto auto";

/** How long a storm went on for, or nothing when it was one knock or an unreadable clock. */
function stormSpan(first, last) {
  const from = instant(first);
  const to = instant(last);
  return Number.isNaN(from) || Number.isNaN(to) || to <= from ? null : `over ${span(from, to)}`;
}

/* -- the knocks ----------------------------------------------------------------------- */

function Knocks({ lines }) {
  return (
    <Rows>
      <Row head columns={KNOCK_COLUMNS}>
        <span>door</span>
        <span>route</span>
        <span>who knocked</span>
        <span>reason</span>
        <span>knocks</span>
        <span>last</span>
      </Row>
      {lines.map((line) => (
        <Row key={line.key} columns={KNOCK_COLUMNS}>
          <Stack sub={line.project}>
            <span className="truncate">{line.agent_id}</span>
          </Stack>
          <Stack sub={line.address}>
            <span className="truncate text-dim">{line.route}</span>
          </Stack>
          <span className="truncate text-dim">{line.from}</span>
          <span className="text-dim">{line.reason}</span>
          {/* The count, not the rows: two hundred knocks from one scanner is one fact, and
              two hundred rows of it would bury the two that are not. */}
          <Badge>{line.count}×</Badge>
          <Stack sub={line.count > 1 ? stormSpan(line.first, line.last) : null}>
            <Clock at={line.last} />
          </Stack>
        </Row>
      ))}
    </Rows>
  );
}

/* -- everything else ------------------------------------------------------------------ */

/**
 * A record drawn from the fields it happens to carry.
 *
 * `DiagnosticWire` is `extra="allow"` and a new kind needs no contract re-record, so this
 * has to render a kind it has never heard of. Dropping one would repeat exactly the failure
 * this page was opened to fix.
 */
function Records({ entries }) {
  return (
    <Rows>
      {/* The index is the key: a snapshot replaces this list wholesale, and a diagnostic
          carries no id of its own. */}
      {entries.map((record, index) => (
        <Row key={index} columns="minmax(0,1fr)">
          <Facts pairs={fields(record)} />
        </Row>
      ))}
    </Rows>
  );
}

/* -- the page ------------------------------------------------------------------------- */

function Head() {
  return (
    <PageHead title="Diagnostics">
      What Chronicle's projection could not fold cleanly, and every knock at a resident's chat
      door that nobody answered. This is the village's own account of itself — steward is not
      asked anything here, so no credential is.
    </PageHead>
  );
}

export default function DiagnosticsPage({ model }) {
  if (!model) {
    return (
      <>
        <Head />
        <Loading>reading the snapshot…</Loading>
      </>
    );
  }

  const diagnostics = model.diagnostics;
  const capacity = model.snapshot.capacity?.diagnostics;
  const groups = groupDiagnostics(diagnostics);
  const brimming = Boolean(capacity) && diagnostics.length >= capacity;

  return (
    <>
      <Head />

      {/* Whether you are looking at everything. A bounded array that has reached its bound is
          no longer the whole story, and a panel that did not say so would be lying quietly. */}
      {diagnostics.length && capacity ? (
        <p className="max-w-[74ch] text-[11.5px] leading-[1.7] text-faint">
          {brimming ? (
            <>
              This channel is <strong className="font-normal text-ink">full</strong>: the
              projection keeps the newest {capacity} records and has already dropped whatever came
              before these. Nothing rate-limits a knock yet (warren#278), so an outsider knocking
              in a loop can push the fleet's own evidence out on their own.
            </>
          ) : (
            <>
              {plural(diagnostics.length, "record")} of the {capacity} this snapshot will keep.
              The oldest are dropped first.
            </>
          )}
        </p>
      ) : null}

      {groups.length ? (
        groups.map((group) => {
          const words = KIND_WORDS[group.kind];
          return (
            <section key={group.kind}>
              <Section count={group.records}>{words ? words.title : group.kind}</Section>
              <p className="mb-[14px] max-w-[74ch] text-[11.5px] leading-[1.7] text-dim">
                {words ? (
                  <>
                    {words.note}
                    {group.kind === UNNAMED ? null : (
                      <>
                        {" "}
                        <code className="text-faint">{group.kind}</code>
                      </>
                    )}
                  </>
                ) : (
                  "Chronicle grew this kind after this page was written, so these are the fields the record carries, unedited."
                )}
              </p>
              {group.kind === KNOCK ? (
                <Knocks lines={group.entries} />
              ) : (
                <Records entries={group.entries} />
              )}
            </section>
          );
        })
      ) : (
        <Empty title="Nothing to report.">
          This snapshot's projection folded every event it was given, every manifest validated,
          and nobody has knocked on a resident's chat door. The village is saying so, which is
          not the same as nobody having looked.
        </Empty>
      )}
    </>
  );
}
