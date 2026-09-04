/* Org — who may hand work to whom, drawn from the manifests rather than by hand.
 *
 * Every line on this page is a fact two declarations already state: `delegation.send` and
 * `delegation.to` on the sending manifest, met by an active route of kind `delegation` on
 * the receiving one. steward computes the whole chart on `GET /org` — the rows included,
 * as a `rank` per node — so this page arranges and labels, and derives nothing. A chart
 * that recomputed the authority would be a second copy of it, and a second copy drifts.
 *
 * The two things worth looking at are the ones a hand-drawn chart cannot show. A resident
 * is a card carrying what it may actually *do* — its API write doors, its rw/ro mounts,
 * its declared cap — because an org chart of agents is a chart of capability, not of
 * seniority. And a grant that will not deliver is still drawn, marked, with steward's own
 * reason beside it: `delegation.to` aimed at a resident whose door is shut is a thing to
 * fix, and a chart that quietly dropped it would answer "no such grant" about a grant
 * sitting in the file.
 */

import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { Gate } from "../console/Gate.jsx";
import { broken, budgetLine, rows, wiring } from "../org.js";
import {
  Badge, Badges, Empty, Loading, PageHead, Panel, Problem, Swatch, Tag,
} from "../console/ui.jsx";

/** One dimension of what a resident may do, always said even when it is empty. */
function Chips({ label, items, tone }) {
  return (
    <div className="mt-[9px]">
      <span className="mr-2 text-[10px] uppercase tracking-[.14em] text-faint">{label}</span>
      {items.length ? (
        items.map((item) => (
          <Tag key={item} tone={tone}>
            {item}
          </Tag>
        ))
      ) : (
        <Tag>none</Tag>
      )}
    </div>
  );
}

function Node({ node, sends, receives }) {
  const mounts = (node.mounts || []).map((mount) => `${mount.container} (${mount.mode})`);
  const managers = receives.map((edge) => edge.sender);
  return (
    <article
      className="min-w-[268px] flex-1 basis-[268px] border bg-deep px-[18px] py-[15px]"
      style={{ "--accent": node.accent || "var(--color-ember)", borderColor: "var(--color-rule)" }}
    >
      <header className="flex items-baseline gap-2.5">
        <Swatch accent={node.accent} className="-translate-y-px" />
        <span className="min-w-0">
          <Link
            to={routeTo.resident(node.id)}
            className="font-serif text-[17px] leading-[1.2] text-ink no-underline hover:text-ember"
          >
            {node.name}
          </Link>{" "}
          <span className="text-[11px] text-faint">{node.id}</span>
          <span className="mt-[3px] block text-[11px] text-dim">{node.role}</span>
        </span>
      </header>

      <Badges className="mt-[10px]">
        {node.retired ? <Badge tone="fail">retired</Badge> : null}
        <Badge tone={node.budget?.declared ? "" : "warn"}>{budgetLine(node.budget)}</Badge>
        {node.delegates ? <Badge>delegates</Badge> : null}
      </Badges>

      <Chips label="grants" items={node.session_grants || []} tone="default" />
      <Chips label="mounts" items={mounts} />
      <Chips label="accepts" items={node.accepts || []} />

      {managers.length ? (
        <p className="mt-[11px] mb-0 text-[10.5px] text-faint">
          takes work from {managers.join(", ")}
        </p>
      ) : null}
      {sends.length ? (
        <p className="mt-[5px] mb-0 text-[10.5px] text-faint">
          hands work to {sends.map((edge) => edge.receiver).join(", ")}
        </p>
      ) : null}
    </article>
  );
}

function Chart({ chart }) {
  const layers = rows(chart);
  const links = wiring(chart);
  const refused = broken(chart);

  if (!layers.length) {
    return (
      <Empty title="No chart to draw.">
        steward could validate no resident, so there is nobody on the chart and no handoff
        between anybody. Anything it refused to read is listed below.
      </Empty>
    );
  }

  return (
    <>
      {layers.map((layer) => (
        <section key={layer.rank} className="mb-5">
          <div className="mb-2 text-[10px] uppercase tracking-[.18em] text-faint">
            {layer.rank === 0 ? "nobody hands work to these" : `one handoff below (rank ${layer.rank})`}
          </div>
          <div className="flex flex-wrap gap-3">
            {layer.nodes.map((node) => (
              <Node
                key={node.id}
                node={node}
                sends={links.sends(node.id)}
                receives={links.receives(node.id)}
              />
            ))}
          </div>
        </section>
      ))}

      {refused.length ? (
        <Panel title="declared, but steward would not carry it">
          <ul className="m-0 list-none p-0">
            {refused.map((edge) => (
              <li key={`${edge.sender}->${edge.receiver}`} className="mb-2 text-[12px] leading-[1.7] text-dim">
                <strong className="text-ink">
                  {edge.sender} → {edge.receiver}
                </strong>{" "}
                — {edge.reason}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </>
  );
}

function Org() {
  const { client } = useSteward();
  const { data, error, loading } = useStewardQuery((signal) => client.readOrg({ signal }), []);

  if (loading && !data) return <Loading>reading the fleet's declarations…</Loading>;
  if (error) return <Problem error={error} />;

  return (
    <>
      {(data?.errors || []).map((line) => (
        <Problem key={line} title="manifest does not validate" error={{ message: line }} />
      ))}
      <Chart chart={data} />
    </>
  );
}

export default function OrgPage() {
  const { locked } = useSteward();
  return (
    <>
      <PageHead title="Org">
        Who may hand work to whom, and what each resident may reach while doing it — computed
        from the manifests on every load, never drawn. An edge exists when a sender declares
        the handoff and the receiver keeps a door open for it; when only one half agrees, the
        edge is still here, marked, with steward's reason.
      </PageHead>
      {locked ? <Gate what="The org chart" /> : <Org />}
    </>
  );
}
