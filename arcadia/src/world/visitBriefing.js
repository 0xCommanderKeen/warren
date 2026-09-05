const KEY = "arcadia:visit:v1";
const LIMIT = 10000;
const STATES = { agents: ["working", "resting", "stale", "failed", "knocking"], tasks: ["open", "claimed", "done", "failed"], approvals: ["pending", "resolved", "collision"] };

function valid(value) {
  if (value?.version !== 1 || typeof value.evaluated_at !== "string" || value.evaluated_at.length > 64 || !Number.isFinite(Date.parse(value.evaluated_at)) ||
    !Number.isSafeInteger(value.generation) || value.generation < 0 || !Number.isSafeInteger(value.log_generation) || value.log_generation < 0) return false;
  let total = 0;
  for (const [field, states] of Object.entries(STATES)) {
    if (!Array.isArray(value[field])) return false;
    total += value[field].length;
    if (total > LIMIT) return false;
    const ids = new Set();
    for (const item of value[field]) {
      if (!Array.isArray(item) || item.length !== 2 || typeof item[0] !== "string" || !item[0] || item[0].length > 512 || ids.has(item[0]) || !states.includes(item[1])) return false;
      ids.add(item[0]);
    }
  }
  return true;
}

export function visitBaseline(snapshot) {
  const value = { version: 1, evaluated_at: snapshot.evaluated_at, generation: snapshot.generation, log_generation: snapshot.log_generation,
    agents: (snapshot.villagers || []).map(agent => [agent.id, agent.state]),
    tasks: (snapshot.tasks || []).map(task => [task.id, task.state]),
    approvals: (snapshot.approvals || []).map(approval => [approval.request_id, approval.state]) };
  return valid(value) ? value : null;
}

export function readVisitBaseline() {
  try {
    const text = localStorage.getItem(KEY);
    const value = text && text.length <= 1000000 ? JSON.parse(text) : null;
    return valid(value) ? value : null;
  } catch { return null; }
}

export function saveVisitBaseline(snapshot) {
  const value = visitBaseline(snapshot);
  if (!value) return;
  try {
    const text = JSON.stringify(value);
    if (text.length <= 1000000) localStorage.setItem(KEY, text);
  } catch { /* Optional browser memory. */ }
}

export function createVisitBriefing(saved = null) {
  let baseline = valid(saved) ? structuredClone(saved) : null;
  let latestGeneration = baseline?.generation ?? -1;
  let acknowledgedApprovals = new Set();
  const acknowledge = snapshot => { acknowledgedApprovals = new Set((snapshot.approvals || []).filter(item => item.state === "pending").map(item => item.request_id)); };
  return {
    markSeen(snapshot) { baseline = visitBaseline(snapshot); latestGeneration = snapshot.generation; acknowledge(snapshot); },
    update(snapshot) {
      const current = visitBaseline(snapshot);
      const empty = { since: current?.evaluated_at ?? null, arrivals: [], completedTasks: [], failures: [], approvals: [] };
      if (!current) return empty;
      if (!baseline || baseline.log_generation !== current.log_generation || current.generation < latestGeneration) {
        baseline = current; latestGeneration = current.generation; acknowledge(snapshot); return empty;
      }
      latestGeneration = current.generation;
      const agents = new Map(baseline.agents), tasks = new Map(baseline.tasks);
      // A retained task first appearing in this window may be old history. Known
      // state changes are direct evidence; new IDs need a timestamp after the visit.
      const recentTask = task => tasks.has(task.id) ||
        (typeof task.updated_at === "string" && Number.isFinite(Date.parse(task.updated_at)) && Date.parse(task.updated_at) >= Date.parse(baseline.evaluated_at));
      return { since: baseline.evaluated_at,
        arrivals: (snapshot.villagers || []).filter(agent => !agents.has(agent.id)),
        completedTasks: (snapshot.tasks || []).filter(task => task.state === "done" && tasks.get(task.id) !== "done" && recentTask(task)),
        failures: [
          ...(snapshot.villagers || []).filter(agent => agent.state === "failed" && agents.get(agent.id) !== "failed").map(record => ({ kind: "agent", record })),
          ...(snapshot.tasks || []).filter(task => task.state === "failed" && tasks.get(task.id) !== "failed" && recentTask(task)).map(record => ({ kind: "task", record })),
        ],
        approvals: (snapshot.approvals || []).filter(approval => approval.state === "pending" && !acknowledgedApprovals.has(approval.request_id)),
      };
    },
  };
}
