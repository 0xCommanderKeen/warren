/* Turning steward's org projection into rows, as pure functions (warren#441).
 *
 * `GET /org` already answers the hard question — who may hand work to whom, and whether
 * the pair of declarations behind each edge actually agree — and it answers it with a
 * `rank` per node so the terminal and this page cannot disagree about who is above whom.
 * Nothing here re-derives any of that. What is left is arranging: group the nodes into
 * rows by rank, and index the edges by the two ends a card needs to name.
 *
 * It lives outside the component for the usual reason: a layout that is a function of the
 * document is testable without rendering anything, and a page that computed it inline
 * would be a page whose only test is a screenshot.
 */

/** The nodes as rows, top rank first, each row sorted by id. */
export function rows(chart) {
  const nodes = chart?.nodes || [];
  const byRank = new Map();
  for (const node of nodes) {
    const rank = Number.isInteger(node.rank) ? node.rank : 0;
    if (!byRank.has(rank)) byRank.set(rank, []);
    byRank.get(rank).push(node);
  }
  return [...byRank.entries()]
    .sort(([left], [right]) => left - right)
    .map(([rank, members]) => ({
      rank,
      nodes: [...members].sort((left, right) => String(left.id).localeCompare(String(right.id))),
    }));
}

/** Every edge out of each resident, and every edge into it, keyed by resident id. */
export function wiring(chart) {
  const edges = chart?.edges || [];
  const out = new Map();
  const into = new Map();
  const push = (index, key, edge) => {
    if (!index.has(key)) index.set(key, []);
    index.get(key).push(edge);
  };
  for (const edge of edges) {
    push(out, edge.sender, edge);
    push(into, edge.receiver, edge);
  }
  return {
    sends: (id) => out.get(id) || [],
    receives: (id) => into.get(id) || [],
  };
}

/** The declared handoffs steward would refuse to carry today. */
export function broken(chart) {
  return (chart?.edges || []).filter((edge) => !edge.deliverable);
}

/**
 * The one-line budget a chip shows.
 *
 * "no cap" rather than nothing, for the same reason steward's own budget view says it out
 * loud: an unlimited resident and a resident nobody has looked at must not render the same.
 */
export function budgetLine(budget) {
  if (!budget) return "no cap";
  const parts = [
    budget.daily_cost_usd ? `$${budget.daily_cost_usd}/day` : null,
    budget.daily_tokens ? `${budget.daily_tokens} tokens/day` : null,
    budget.max_run_seconds ? `${budget.max_run_seconds}s/run` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "no cap";
}
