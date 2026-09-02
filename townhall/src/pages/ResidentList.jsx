import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import {
  Badge, Badges, Clock, Empty, Gauge, Loading, Problem, Row, Rows, Stack, Who,
} from "../console/ui.jsx";
import { soonest } from "../console/time.js";

const COLUMNS = "1.5fr .85fr .95fr 1.1fr 1.15fr";

/**
 * The soonest upcoming fire of one resident's routines (#155).
 *
 * `next_fire` comes back in the routine's *own* `schedule_tz`, so a fleet spanning zones
 * produces strings with different offsets and the console's `localeCompare` ordered the
 * local wall-clock text rather than the instants — naming a fire that was not the soonest
 * whenever the offsets disagreed. `soonest` parses before it compares.
 */
function nextFireOf(resident, routines) {
  const mine = (routines || []).filter((row) => row.resident === resident.id);
  return soonest(mine, (row) => row.next_fire)?.next_fire ?? null;
}
/* -- the list ------------------------------------------------------------------------ */

export default function ResidentList() {
  const { client } = useSteward();
  const { data, error, loading } = useStewardQuery(
    (signal) =>
      Promise.all([
        client.listResidents({ signal }),
        // The fleet ledger, for the "next fire" sub-line. Settled rather than awaited: a
        // residents list that vanished because the scheduler ledger was unreadable would be
        // a worse answer than one that simply says nothing about next fires.
        client.listRoutines({ signal }).then(
          (value) => value,
          () => null,
        ),
      ]).then(([listing, ledger]) => ({ ...listing, routines: ledger?.routines || [] })),
    [],
  );

  if (loading) return <Loading>reading the fleet…</Loading>;
  if (error) return <Problem error={error} />;

  const residents = data?.residents || [];
  const routines = data?.routines || [];

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
              {/* Addressed by uid, not id — see routeTo.resident. */}
              <Link to={routeTo.resident(resident.uid)} className="no-underline text-inherit">
                <Who
                  accent={resident.soul.accent}
                  name={resident.soul.name}
                  id={resident.id}
                  role={resident.soul.role}
                  retired={resident.retired}
                />
              </Link>
              <Stack sub={resident.runner.model || "no model named"}>{resident.runner.kind}</Stack>
              <Stack
                sub={
                  <>
                    {`${resident.skills.length} granted`}
                    {nextFireOf(resident, routines) ? (
                      <>
                        {" · next "}
                        <Clock at={nextFireOf(resident, routines)} mode="until" />
                      </>
                    ) : null}
                  </>
                }
              >
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
