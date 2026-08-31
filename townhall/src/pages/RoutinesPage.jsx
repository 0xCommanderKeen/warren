/* Every routine every valid resident declares, fleet-wide — and whether anything fires them.
 *
 * The console's Routines view, ported (warren#225). An enabled routine is a *declaration*,
 * not a heartbeat, so the heartbeat is on the page beside it: steward reports `last_tick`,
 * `stale_after_s` and a verdict on `GET /routines`, with `alive: null` — nothing has ever
 * ticked — as its own answer. A page full of next-fire promises that could not say whether
 * anybody keeps them would be worse than no page.
 *
 * The **last run** column is warren#104's fix. The console showed `last_request`, which is
 * the API request log: only a run somebody asked for over HTTP leaves a row there, so a
 * resident firing perfectly on its schedule read as one that had never run. steward now
 * returns `last_run` from its run ledger beside it, with the trigger and the outcome. Both
 * columns are here because their disagreement is the diagnosis — an accepted request with
 * no run behind it is exactly the case worth seeing.
 */

import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { Gate } from "../console/Gate.jsx";
import {
  Badge, Clock, Empty, Loading, PageHead, Problem, Row, Rows, Stack, Swatch,
} from "../console/ui.jsx";
import {
  LastRequest, LastRun, ROUTINE_COLUMNS, RunButton, SchedulerBadge, SchedulerNote,
} from "../console/routines.jsx";
import { RETIRED_REFUSAL } from "../console/routines.jsx";

function Ledger() {
  const { client } = useSteward();
  const { data, error, loading, refresh } = useStewardQuery(
    (signal) => client.listRoutines({ signal }),
    [],
  );

  if (loading && !data) return <Loading>reading the standing work…</Loading>;
  if (error) return <Problem error={error} />;

  const routines = data?.routines || [];
  const scheduler = data?.scheduler || null;

  return (
    <>
      <p className="mb-6 max-w-[78ch] text-[12.5px] leading-[1.7] text-dim">
        <SchedulerBadge scheduler={scheduler} />
        <SchedulerNote scheduler={scheduler} />
      </p>

      {(data?.errors || []).map((line) => (
        <Problem key={line} title="manifest does not validate" error={{ message: line }} />
      ))}

      {routines.length ? (
        <Rows className="rise">
          <Row head columns={ROUTINE_COLUMNS}>
            <span>resident</span>
            <span>routine · schedule</span>
            <span>next</span>
            <span>last run</span>
            <span>last asked-for run</span>
            <span />
          </Row>
          {routines.map((row) => (
            <Row key={row.key} columns={ROUTINE_COLUMNS} accent={row.accent}>
              <Link
                to={routeTo.resident(row.resident)}
                className="min-w-0 text-inherit no-underline"
              >
                <span className="flex min-w-0 items-baseline gap-2.5">
                  <Swatch accent={row.accent} className="-translate-y-px" />
                  <span className="min-w-0">
                    <span className="block truncate font-serif text-[15px]">{row.resident_name}</span>
                    <span className="text-[11px] text-faint">{row.resident}</span>
                  </span>
                </span>
              </Link>
              <Stack
                sub={
                  <>
                    {row.schedule} · {row.schedule_tz}
                  </>
                }
              >
                <span className="flex flex-wrap items-baseline gap-1.5">
                  {row.routine}
                  {row.enabled ? null : <Badge tone="fail">disabled</Badge>}
                  {row.retired ? <Badge tone="fail">retired</Badge> : null}
                </span>
              </Stack>
              <Stack sub={row.anchor ? <>anchored <Clock at={row.anchor} /></> : "never fired"}>
                {row.retired ? (
                  <span className="text-faint" title={RETIRED_REFUSAL}>
                    never fires
                  </span>
                ) : row.enabled ? (
                  <Clock at={row.next_fire} mode="until" />
                ) : (
                  <span className="text-faint">no next fire</span>
                )}
              </Stack>
              <LastRun run={row.last_run} />
              <LastRequest request={row.last_request} />
              <RunButton row={row} client={client} onSettled={refresh} />
            </Row>
          ))}
        </Rows>
      ) : (
        <Empty title="No standing work anywhere.">
          No valid resident declares a routine. Either the fleet only ever works on demand —
          board claims and delegated inboxes — or the residents tree steward was pointed at is
          empty.
        </Empty>
      )}

      {routines.length ? (
        <p className="mt-6 max-w-[78ch] text-[11.5px] leading-[1.7] text-faint">
          Scheduler state read from <code>{data.state_path}</code>. <strong>Last run</strong> is
          steward's run ledger — every finished session, however it was started.{" "}
          <strong>Last asked-for run</strong> is the request log, which only ever holds runs
          somebody asked for through this API. A routine firing on its own schedule shows in
          the first and never in the second.
        </p>
      ) : null}
    </>
  );
}

export default function RoutinesPage() {
  const { locked } = useSteward();
  return (
    <>
      <PageHead title="Routines">
        Every routine every valid resident declares, fleet-wide. The anchor is the moment the
        next occurrence is computed from: the last fire, or when steward first saw the routine.
        A retired resident's routines are still listed and never fire — they are what used to
        run here, which is a question a ledger should be able to answer.
      </PageHead>
      {locked ? <Gate what="The standing-work ledger" /> : <Ledger />}
    </>
  );
}
