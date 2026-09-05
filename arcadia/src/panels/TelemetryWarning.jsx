/** Chronicle ages producer observations; this view never derives agent activity. */
export function TelemetryWarning({ snapshot }) {
  const affected = (snapshot.producer_health || []).filter((source) => source.status !== 'healthy');
  const uncertain = snapshot.villagers?.filter((agent) => agent.presence?.freshness === 'unknown') || [];
  if (!affected.length && !uncertain.length) return null;
  const evaluated = Date.parse(snapshot.evaluated_at) / 1000;
  return <aside className="telemetry-warning" role="status" aria-label="Producer telemetry">
    <strong>Current activity is uncertain</strong>
    <p>Telemetry is delayed or unavailable. Agent details show last-observed activity.</p>
    <ul>{affected.map((source) => <li key={`${source.producer}:${source.target}`}>
      Source {source.producer} · {source.status} · {source.queue_depth} queued · evidence {Math.max(0, Math.floor(evaluated - source.observed_at))}s old
      {source.oldest_at != null && ` · oldest queued ${Math.max(0, Math.floor(evaluated - source.oldest_at))}s`}
      {!!source.overflow && ` · ${source.overflow} records lost at capacity`}
    </li>)}{uncertain.map((agent) => <li key={agent.id}>
      {agent.name} · activity unknown · last observed {Math.max(0, Math.floor(evaluated - agent.presence.observed_at))}s ago
    </li>)}</ul>
  </aside>;
}
