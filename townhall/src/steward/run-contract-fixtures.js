/* Representative responses shared by rendering and exported-schema contract tests. */

export const ROUTINE = {
  key: "hob/daily-summary",
  resident: "hob",
  resident_name: "Hob",
  accent: "#a68a4f",
  routine: "daily-summary",
  schedule: "0 9 * * *",
  schedule_tz: "Europe/Ljubljana",
  enabled: true,
  retired: false,
  requires: [],
  timeout_s: 600,
  journal: null,
  anchor: "2026-08-30T07:00:00+00:00",
  next_fire: "2026-09-01T09:00:00+02:00",
  last_request: null,
  last_run: {
    run_id: "run-7",
    trigger: "schedule",
    outcome: "ok",
    recorded_at: "2026-08-31T07:00:41.220Z",
    duration_s: 41.2,
  },
};

export const ROUTINES = {
  routines: [ROUTINE],
  state_path: "/srv/steward/.steward/state/scheduler.json",
  scheduler: { last_tick: "2026-08-31T09:31:02+00:00", stale_after_s: 360, alive: true },
  errors: [],
};

export const RUN_RECEIPT = {
  request_id: "request-7", status: "accepted", resident: "hob", routine: "daily-summary",
  trigger: "manual", message: "queued one run; read the request log for the outcome",
};

export const RUN_REQUEST = {
  request_id: "request-7", received_at: "2026-08-31T07:00:00+00:00", method: "POST",
  path: "/residents/hob/routines/daily-summary/run", outcome: "queued",
  detail: { routine: "hob/daily-summary" },
};
