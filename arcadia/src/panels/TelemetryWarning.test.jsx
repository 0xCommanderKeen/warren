import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { TelemetryWarning } from './TelemetryWarning.jsx';
afterEach(cleanup);
it('qualifies last-observed activity and names the delayed source', () => {
  render(<TelemetryWarning snapshot={{ evaluated_at: '2026-09-05T20:00:00Z',
    producer_health: [{ producer: 'laptop-source', target: 'nas', status: 'delayed',
      observed_at: Date.parse('2026-09-05T19:59:55Z') / 1000,
      oldest_at: Date.parse('2026-09-05T19:58:00Z') / 1000, queue_depth: 20 }] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('laptop-source');
  expect(screen.getByRole('status')).toHaveTextContent('Current activity is uncertain');
  expect(screen.getByRole('status')).toHaveTextContent('120s');
});
it('does not warn for fresh healthy telemetry', () => {
  render(<TelemetryWarning snapshot={{ producer_health: [{status: 'healthy'}] }} />);
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
