export class UnsupportedSchemaVersionError extends Error {
  constructor(version) {
    super(`Unsupported village schema version: ${String(version)}`);
    this.name = "UnsupportedSchemaVersionError";
  }
}

export function parseSnapshot(envelope) {
  if (envelope?.kind !== "snapshot" && envelope?.kind !== "reset") {
    throw new TypeError("Expected a Burrow snapshot or reset envelope");
  }

  const snapshot = envelope.snapshot;

  if (snapshot?.schema_version !== 1) {
    throw new UnsupportedSchemaVersionError(snapshot?.schema_version);
  }

  const collections = [
    "villagers", "artifacts", "tasks", "approvals", "routines", "residents", "journals",
  ];
  for (const collection of collections) {
    if (!Array.isArray(snapshot[collection])) {
      throw new TypeError(`Expected snapshot.${collection} to be an array`);
    }
  }

  return snapshot;
}
