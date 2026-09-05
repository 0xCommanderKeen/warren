export class UnsupportedSchemaVersionError extends Error {
  constructor(version) {
    super(`Unsupported village schema version: ${String(version)}`);
    this.name = "UnsupportedSchemaVersionError";
  }
}

export class ContractValidationError extends Error {
  constructor(path, expected) {
    super(`Expected ${path} to be ${expected}`);
    this.name = "ContractValidationError";
    this.path = path;
  }
}

function invalid(path, expected) {
  throw new ContractValidationError(path, expected);
}

const string = (value, path) => {
  if (typeof value !== "string") invalid(path, "a string");
};
const boolean = (value, path) => {
  if (typeof value !== "boolean") invalid(path, "a boolean");
};
const number = (value, path) => {
  if (typeof value !== "number" || !Number.isFinite(value)) invalid(path, "a finite number");
};
const integer = (value, path) => {
  if (!Number.isInteger(value)) invalid(path, "an integer");
};
const literal = (...allowed) => (value, path) => {
  if (!allowed.includes(value)) invalid(path, allowed.map(JSON.stringify).join(" or "));
};
const nullable = (validate) => (value, path) => {
  if (value !== null) validate(value, path);
};

function object(value, path) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    invalid(path, "an object");
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) invalid(path, "a plain object");
}

function descend(value, path, context, visit) {
  if (context.active.has(value)) invalid(path, "acyclic JSON data");
  context.active.add(value);
  try {
    visit();
  } finally {
    context.active.delete(value);
  }
}

const array = (validate) => (value, path, context) => {
  if (!Array.isArray(value)) invalid(path, "an array");
  descend(value, path, context, () => {
    value.forEach((item, index) => validate(item, `${path}[${index}]`, context));
  });
};

const record = (validate) => (value, path, context) => {
  object(value, path);
  descend(value, path, context, () => {
    Object.entries(value).forEach(([key, item]) => validate(item, `${path}.${key}`, context));
  });
};

function jsonValue(value, path, context) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") return number(value, path);
  if (Array.isArray(value)) return array(jsonValue)(value, path, context);
  if (typeof value === "object") return record(jsonValue)(value, path, context);
  invalid(path, "a JSON value");
}

function model(fields, { allowExtra = true, optional = [] } = {}) {
  const optionalFields = new Set(optional);
  return (value, path, context) => {
    object(value, path);
    descend(value, path, context, () => {
      for (const [name, validate] of Object.entries(fields)) {
        if (!Object.hasOwn(value, name)) {
          if (!optionalFields.has(name)) invalid(`${path}.${name}`, "present");
          continue;
        }
        validate(value[name], `${path}.${name}`, context);
      }
      if (!allowExtra) {
        const unknown = Object.keys(value).find((name) => !Object.hasOwn(fields, name));
        if (unknown !== undefined) invalid(`${path}.${unknown}`, "a declared version-1 field");
      }
    });
  };
}

const protocolEvent = model({
  v: integer, ts: string, source: string, agent_id: string, project: string,
  cwd: nullable(string), type: string, payload: record(jsonValue),
}, { optional: ["cwd"] });

const presence = model({
  agent_id: string, session_id: string, epoch: integer, sequence: integer,
  observed_at: number, source: literal("codex", "claude-code"), project: string,
  state: literal("working", "resting", "knocking", "failed", "ended"), producer: string,
  freshness: literal("fresh", "unknown"), expires_at: number,
});
const producerHealth = model({
  producer: string, target: string, observed_at: number, queue_depth: integer,
  oldest_at: nullable(number), last_success: nullable(number),
  error: nullable(literal("dns", "connect", "timeout", "authentication", "invalid_event", "http", "disk", "worker")),
  retry_at: number, failures: integer, worker: literal("running"), overflow: integer,
  presence_overflow: integer, status: literal("healthy", "delayed", "unknown", "overloaded"),
  expires_at: number, received_at: number,
});

const villager = model({
  id: string, name: string, char: string, accent: string,
  residency: literal("resident", "visitor"), home: nullable(integer),
  base: literal("home", "lodge"), resident_file: nullable(string),
  state: literal("knocking", "resting", "failed", "stale", "working"),
  project: string, cwd: string, last_ts: string, last_line: string, place: nullable(string),
  lineage: record(string), history: array(protocolEvent), mood: record(jsonValue),
  pending_approval_ids: array(string), presence: nullable(presence),
}, { optional: ["presence"] });

const resident = model({
  file: string, valid: literal(true), manifest_version: literal(1), match: record(string),
  home: integer, meta: record(string), body: string, capabilities: record(jsonValue),
  routines: array(record(jsonValue)),
});

const diagnosticResident = model({
  file: string, valid: literal(false), diagnostic: literal(true),
  manifest_version: nullable(integer), match: record(string), declared_home: nullable(integer),
  meta: record(string), body: nullable(string), capabilities: record(jsonValue),
});

const artifact = model({ agent_id: string, project: string, artifact: string, ts: string });
const task = model({
  id: string, title: string, state: literal("open", "claimed", "done", "failed"),
  required_skills: array(string), posted_by: string, assignee: nullable(string),
  claimant: nullable(string), updated_at: string,
});
const approval = model({
  request_id: string, agent_id: string, project: string,
  state: literal("pending", "resolved", "collision"), message: string,
  action: nullable(string), detail: jsonValue, options: array(jsonValue),
  expires_at_present: boolean, expires_at: nullable(string), opened_at: string,
  decision: nullable(string), resolved_at: nullable(string),
}, { optional: ["decision", "resolved_at"] });
const journal = model({
  day: string, agent_id: string, project: string, source: string, routine: string,
  path: string, observed_at: string,
});
const routine = model({
  run_id: string, routine: string, agent_id: string, project: string, source: string,
  state: literal("running", "finished", "failed"), trigger: string, started_at: string,
  updated_at: string, outcome: nullable(string), duration_s: nullable(number),
  artifacts: array(jsonValue), error: nullable(string),
});
const diagnostic = model({
  kind: nullable(string), file: nullable(string), path: nullable(string), message: nullable(string),
}, { optional: ["kind", "file", "path", "message"] });
const capacity = model({
  villagers: integer, events_per_villager: integer, ambient_events_per_villager: integer,
  tasks: integer, approvals: integer, journals: integer, routines: integer,
  diagnostics: integer, ambient_diagnostics: integer,
});

const villageState = model({
  schema_version: literal(1, 2), generation: integer, cursor: string, log_generation: integer,
  evaluated_at: string, villagers: array(villager), residents: array(resident),
  diagnostic_residents: array(diagnosticResident), artifacts: array(artifact), tasks: array(task),
  approvals: array(approval), journals: array(journal), routines: array(routine),
  diagnostics: array(diagnostic), capacity, capabilities: record(boolean),
  producer_health: array(producerHealth),
}, { allowExtra: false, optional: ["producer_health"] });

export function parseSnapshot(envelope) {
  if (envelope?.kind !== "snapshot" && envelope?.kind !== "reset") {
    invalid("envelope.kind", '"snapshot" or "reset"');
  }
  const snapshot = envelope.snapshot;
  if (![1, 2].includes(snapshot?.schema_version)) {
    throw new UnsupportedSchemaVersionError(snapshot?.schema_version);
  }
  villageState(snapshot, "snapshot", { active: new WeakSet() });
  return snapshot;
}
