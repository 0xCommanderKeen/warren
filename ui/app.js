/* steward — operator console.
 *
 * A pure client. Every fact on the screen was fetched from the API a moment ago, and
 * nothing on the screen is written by this file on the fleet's behalf. The rule that
 * shapes the whole thing: the API answers *accepted*, never *done*, so an action here
 * moves through asked → accepted → confirmed, and only steward's own request log,
 * board, or approval record may move it to the last one. There is no optimistic state.
 *
 * No framework, no build step, no CDN. Text goes into the DOM as text nodes, never as
 * markup, so a resident's name or an API message cannot become script.
 */

"use strict";

/* ------------------------------------------------------------------------------------
 * every path this console calls
 *
 * The one list, and the whole list — `call()` below is the only place a fetch happens.
 * tests/test_ui.py parses these literals and asserts each is a real route on the API, so
 * a typo in this map fails Python's test run rather than a panel at 2am.
 * ---------------------------------------------------------------------------------- */

const ROUTES = {
  residents:        "/residents",
  resident:         "/residents/{resident_id}",
  journal:          "/residents/{resident_id}/journal",
  budget:           "/residents/{resident_id}/budget",
  inbox:            "/residents/{resident_id}/inbox",
  runRoutine:       "/residents/{resident_id}/routines/{routine_id}/run",
  routines:         "/routines",
  skills:           "/skills",
  jobs:             "/jobs",
  approvals:        "/approvals",
  approval:         "/approvals/{request_id}",
  request:          "/requests/{request_id}",
};

/* ------------------------------------------------------------------------------------
 * the token
 * ---------------------------------------------------------------------------------- */

const TOKEN_KEY = "steward.token";
const REPROMPT = Symbol("reprompt");

const gate = document.getElementById("gate");
const gateInput = document.getElementById("gateinput");
const gateError = document.getElementById("gateerror");

function storedToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

function forgetToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

let gateWaiters = [];

function askForToken(reason) {
  if (reason) {
    gateError.textContent = reason;
    gateError.hidden = false;
  } else {
    gateError.hidden = true;
  }
  gateInput.value = "";
  if (!gate.open) gate.showModal();
  setLink("busy", "waiting for a token");
  return new Promise((resolve) => gateWaiters.push(resolve));
}

// Escape must not dismiss the gate: a console with no token has nothing to show, and a
// silently closed dialog would leave a person staring at a blank page wondering why.
gate.addEventListener("cancel", (event) => event.preventDefault());

document.getElementById("gateform").addEventListener("submit", (event) => {
  const open = event.submitter && event.submitter.id === "gateopen";
  const value = gateInput.value.trim();
  if (!open && !value) {
    event.preventDefault();
    gateError.textContent =
      "Paste the token, or say out loud that this server runs open (steward serve --allow-open).";
    gateError.hidden = false;
    return;
  }
  // An open server is stored as the empty string: known, and known to be nothing.
  sessionStorage.setItem(TOKEN_KEY, open ? "" : value);
  const waiters = gateWaiters;
  gateWaiters = [];
  setTimeout(() => waiters.forEach((resolve) => resolve()), 0);
});

document.getElementById("forget").addEventListener("click", async () => {
  forgetToken();
  setLink("", "forgotten");
  await askForToken("Token forgotten. This tab holds nothing until you paste one again.");
  render();
});

/* ------------------------------------------------------------------------------------
 * the one fetch
 * ---------------------------------------------------------------------------------- */

class ApiError extends Error {
  constructor(status, code, message, raw) {
    super(message);
    this.status = status;
    this.code = code;
    this.raw = raw;
  }
}

function fill(template, params) {
  return template.replace(/\{(\w+)\}/g, (_, key) => {
    if (!params || params[key] === undefined) throw new Error(`${template} needs ${key}`);
    return encodeURIComponent(params[key]);
  });
}

/** Read or write one endpoint. The only fetch in this console. */
async function call(name, options = {}) {
  const template = ROUTES[name];
  if (!template) throw new Error(`no route named ${name}`);
  let token = storedToken();
  if (token === null) {
    await askForToken(null);
    token = storedToken();
  }

  const query = options.query
    ? "?" + new URLSearchParams(options.query).toString()
    : "";
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(apiBase() + fill(template, options.params) + query, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (cause) {
    if (options.signal && options.signal.aborted) throw cause;
    setLink("bad", "unreachable");
    throw new ApiError(0, "unreachable", `the API did not answer: ${cause.message}`, null);
  }

  if (response.status === 401) {
    forgetToken();
    await askForToken(
      token
        ? "That token was refused. steward compares it against STEWARD_TOKEN in its own environment."
        : "This server wants a token. Paste the value of STEWARD_TOKEN."
    );
    throw REPROMPT;
  }

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    setLink("bad", `api said ${response.status}`);
    const detail = payload && payload.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      throw new ApiError(response.status, detail.error || "error", detail.message || text, payload);
    }
    if (Array.isArray(detail)) {
      const lines = detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("\n");
      throw new ApiError(response.status, "invalid_body", lines || text, payload);
    }
    throw new ApiError(response.status, `http_${response.status}`, text || response.statusText, payload);
  }

  setLink("ok", "steward answering");
  return payload;
}

/** Where the API lives. The console is served from it, so that is simply here. */
function apiBase() {
  return window.location.pathname.replace(/\/ui\/?.*$/, "");
}

const link = document.getElementById("link");
const linkText = document.getElementById("linktext");

function setLink(kind, words) {
  link.className = "linkstate" + (kind ? " " + kind : "");
  linkText.textContent = words;
}

/* ------------------------------------------------------------------------------------
 * DOM
 * ---------------------------------------------------------------------------------- */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "style") Object.assign(node.style, value);
    else if (key === "on") for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  add(node, children);
  return node;
}

function add(node, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function frag(...children) {
  const f = document.createDocumentFragment();
  add(f, children);
  return f;
}

/** Stagger a section's arrival. One orchestrated entrance per view, and nothing after. */
function rise(node, index) {
  node.classList.add("rise");
  node.style.animationDelay = `${Math.min(index, 9) * 42}ms`;
  return node;
}

/* ------------------------------------------------------------------------------------
 * small pieces
 * ---------------------------------------------------------------------------------- */

function head(title, ...standfirst) {
  return el("div", { class: "viewhead rise" },
    el("h1", {}, title),
    el("p", { class: "standfirst" }, ...standfirst)
  );
}

function label(text) {
  return el("span", { class: "label" }, text);
}

function badge(text, kind) {
  return el("span", { class: "badge " + (kind || "") }, el("span", { class: "sq" }), text);
}

function tag(text, kind) {
  return el("span", { class: "tag " + (kind || "") }, text);
}

function empty(title, why) {
  return el("div", { class: "empty" }, el("strong", {}, title), el("p", {}, why));
}

function problem(error) {
  if (error instanceof ApiError) {
    return el("div", { class: "problem" },
      el("div", { class: "code" }, `${error.status || "no answer"} · ${error.code}`),
      el("div", { class: "msg" }, error.message),
      error.raw
        ? el("details", {},
            el("summary", {}, "the response, verbatim"),
            el("pre", {}, JSON.stringify(error.raw, null, 2)))
        : null
    );
  }
  return el("div", { class: "problem" },
    el("div", { class: "code" }, "console fault"),
    el("div", { class: "msg" }, error && error.message ? error.message : String(error))
  );
}

function facts(pairs) {
  const list = el("dl", { class: "facts" });
  for (const [term, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    add(list, [el("dt", {}, term), el("dd", {}, value)]);
  }
  return list;
}

function section(title, count) {
  return el("h2", { class: "section" }, title,
    count === undefined ? null : el("span", { class: "count" }, `(${count})`));
}

function mono(text) {
  return el("span", { class: "wrap" }, text);
}

function jsonBlock(value) {
  return el("pre", {}, JSON.stringify(value, null, 2));
}

/* -- time, told honestly ------------------------------------------------------------- */

function stamp(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleString([], {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function span(fromMs, toMs) {
  const seconds = Math.max(0, Math.round((toMs - fromMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

/** A clock element that keeps itself honest: rewritten once a second, forever. */
function clock(iso, mode) {
  if (!iso) return el("span", { class: "faint" }, "—");
  const node = el("time", { datetime: iso, title: stamp(iso), "data-clock": mode }, "");
  node.dataset.at = iso;
  tickClock(node);
  return node;
}

function tickClock(node) {
  const at = new Date(node.dataset.at).getTime();
  if (Number.isNaN(at)) { node.textContent = node.dataset.at; return; }
  const now = Date.now();
  if (node.dataset.clock === "until") {
    node.textContent = at <= now ? `expired ${span(at, now)} ago` : `in ${span(now, at)}`;
    node.classList.toggle("faint", at <= now);
  } else {
    node.textContent = at <= now ? `${span(at, now)} ago` : `in ${span(now, at)}`;
  }
}

setInterval(() => document.querySelectorAll("time[data-clock]").forEach(tickClock), 1000);

/* -- the scheduler's heartbeat -------------------------------------------------------- */

/**
 * Read whether a scheduler is up. The console never infers this: `GET /routines` carries
 * steward's own `{last_tick, stale_after_s, alive}`, and `alive: null` — nothing has ever
 * ticked — is a third answer, not a missing one. A read that fails is `null` here, which
 * every caller below renders as "unknown" rather than as bad news.
 */
async function schedulerLiveness(signal) {
  try {
    return (await call("routines", { signal })).scheduler || null;
  } catch {
    return null;
  }
}

/** The badge, so a page of next-fire promises says up front whether anything keeps them. */
function schedulerBadge(scheduler) {
  if (!scheduler) return badge("scheduler unknown", "");
  if (scheduler.alive === null) return badge("scheduler has never ticked", "fail");
  if (!scheduler.alive) return badge("scheduler not up", "fail");
  return badge("scheduler up", "live");
}

/** The badge's fine print: when the last tick was, and what it means for what is listed. */
function schedulerNote(scheduler) {
  if (!scheduler) {
    return frag(" — this steward reports no heartbeat, so nothing here says whether the " +
      "next fires below will happen.");
  }
  if (scheduler.alive === null) {
    return frag(" — nothing has ever ticked steward's state file, so every next fire " +
      "below is a promise with nobody to keep it.");
  }
  if (!scheduler.alive) {
    return frag(" — last tick ", clock(scheduler.last_tick, "ago"), ", older than ",
      span(0, scheduler.stale_after_s * 1000),
      ": nothing below fires until steward scheduler run is up again.");
  }
  return frag(" — last tick ", clock(scheduler.last_tick, "ago"),
    ", so what is listed below really does fire.");
}

/** The same fact as a sentence, for a pending action that steward has gone quiet on. */
function schedulerBlame(scheduler) {
  if (!scheduler) {
    return "It is still queued, or whatever would run it is not up — steward's heartbeat " +
      "could not be read.";
  }
  if (scheduler.alive === null) {
    return "No scheduler has ever ticked steward's state file, so a scheduled run has " +
      "nothing to fire it.";
  }
  if (!scheduler.alive) {
    return `No scheduler has ticked since ${stamp(scheduler.last_tick)}, so a scheduled ` +
      "run has nothing to fire it.";
  }
  return `A scheduler ticked at ${stamp(scheduler.last_tick)}, so it is queued or still ` +
    "running rather than unattended.";
}

/* ------------------------------------------------------------------------------------
 * the pending ledger
 *
 * Every mutating action lands here first and stays until steward's own records say what
 * became of it. `confirm` is a function that re-reads steward — the request log, the
 * board, the approval record — and returns a verdict or null for "still nothing".
 * Nothing in this file may write "confirmed" without one of those reads.
 * ---------------------------------------------------------------------------------- */

const ledger = document.getElementById("ledger");
const POLL_MS = 2000;
const POLL_LIMIT = 90;      // three minutes of asking, then it says so and stops

function ticket({ what, requestId, why, confirm, refused }) {
  let cancelled = false;
  let pollTimer = null;
  let controller = null;
  const state = el("span", { class: "state" }, "asked");
  const reason = el("div", { class: "why" }, why || "steward accepted the request.");
  const dismiss = () => {
    cancelled = true;
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (controller !== null) controller.abort();
    node.remove();
  };
  const node = el("div", { class: "tick", "data-state": "asked" },
    el("div", { class: "top" },
      el("span", { class: "what" }, what),
      el("span", { class: "actions" }, state,
        el("button", { class: "dismiss", type: "button", title: "dismiss",
          on: { click: dismiss } }, "×"))),
    reason,
    requestId ? el("div", { class: "rid" }, `request ${requestId}`) : null
  );
  ledger.append(node);

  const settle = (verdict) => {
    if (cancelled || !node.isConnected) return;
    node.dataset.state = verdict.state;
    state.textContent = verdict.state;
    reason.textContent = verdict.why;
    // Catch the view up with whatever steward now says — except on a form, where a
    // re-render would sweep away the answer the person is still reading.
    if (parseHash().view !== "new") render();
  };

  if (refused) {
    node.dataset.state = "failed";
    state.textContent = "refused";
    return node;
  }

  node.dataset.state = "accepted";
  state.textContent = "accepted";
  reason.textContent = why || "accepted, not yet confirmed.";
  if (!confirm) return node;

  let tries = 0;
  const poll = async () => {
    if (cancelled) return;
    tries += 1;
    let verdict = null;
    const activeController = new AbortController();
    controller = activeController;
    try {
      try {
        verdict = await confirm(activeController.signal);
      } catch (error) {
        if (cancelled) return;
        if (error === REPROMPT) return;
        settle({ state: "failed", why: `could not read back what happened: ${error.message}` });
        return;
      }
      if (cancelled) return;
      if (verdict) { settle(verdict); return; }
      if (tries >= POLL_LIMIT) {
        // Three minutes of silence used to be explained with a guess. Ask steward instead:
        // its heartbeat is a fact, and "a scheduler ticked a second ago" and "none ever has"
        // are very different reasons for the same silence.
        const scheduler = await schedulerLiveness(activeController.signal);
        if (!cancelled && node.isConnected) {
          reason.textContent = "accepted, and steward has recorded no outcome in three " +
            "minutes. " + schedulerBlame(scheduler);
        }
        return;
      }
      pollTimer = setTimeout(poll, POLL_MS);
    } finally {
      if (controller === activeController) controller = null;
    }
  };
  pollTimer = setTimeout(poll, 600);
  return node;
}

/** Confirm a run-now the only honest way: read the request log steward wrote it into. */
function confirmRun(requestId) {
  return async (signal) => {
    const record = await call("request", { params: { request_id: requestId }, signal });
    if (record.outcome === "queued") return null;
    const detail = record.detail || {};
    if (record.outcome === "ran") {
      return { state: "confirmed", why: `steward's log: ran${detail.run_id ? ` (run ${detail.run_id})` : ""}.` };
    }
    return {
      state: "failed",
      why: `steward's log: ${record.outcome}${detail.error ? ` — ${detail.error}` : ""}`,
    };
  };
}

/** Confirm a posted job by finding it on the board steward keeps. */
function confirmJob(taskId) {
  return async (signal) => {
    const board = await call("jobs", { signal });
    const job = (board.jobs || []).find((item) => item.task_id === taskId);
    if (!job) return null;
    return {
      state: "confirmed",
      why: `on the board, status ${job.status}. No resident has been prompted — one claims ` +
           `it on its own next wake-up, and task_claimed is the only proof of that.`,
    };
  };
}

/** Confirm a decision by reading the approval record back. */
function confirmApproval(requestId) {
  return async (signal) => {
    const record = await call("approval", { params: { request_id: requestId }, signal });
    if (record.status === "pending") return null;
    return {
      state: "confirmed",
      why: `recorded: ${record.decision} by ${record.decided_by} at ${stamp(record.decided_at)}.`,
    };
  };
}

/** Confirm a declaration by seeing the new manifest come back through the validator.
 *
 * `answer` is the 201 body, which carries the nursery's own report of what it did. The
 * confirmation itself is still a read — the resident has to come back through
 * `GET /residents` — and everything said about the host afterwards is quoted off that
 * report rather than assumed from the status code.
 */
function confirmDeclared(answer) {
  return async (signal) => {
    const listing = await call("residents", { signal });
    const found = (listing.residents || []).some((item) => item.id === answer.id);
    if (!found) return null;

    const provision = answer.provision;
    if (!provision) {
      return {
        state: "confirmed",
        why: "the manifest validates and steward can read it. Nothing is deployed and no " +
             "routine is scheduled: commit the files and deploy from the CLI.",
      };
    }

    const host = provision.target ? provision.target.container : answer.id;
    const did = provision.sent
      ? `the bundle went to ${host} and steward ran ${provision.commands.length} command` +
        `${provision.commands.length === 1 ? "" : "s"} there`
      : `nothing was uploaded to ${host}: the host already had this bundle, which is what ` +
        `a converged re-run looks like`;
    const register = answer.register;
    if (register && register.ok === false) {
      return {
        state: "failed",
        why: `${did}, but the schedule check did not pass: ${register.problems.join("; ")}`,
      };
    }
    const fires = (register && register.next_fires) || [];
    return {
      state: "confirmed",
      why: `the manifest validates and steward can read it; ${did}. ` + (fires.length
        ? `Next fires: ${fires.map((fire) => `${fire.routine} at ${fire.at}`).join(", ")}.`
        : "No enabled routine, so this resident fires nothing on a schedule.") +
        " It appears in burrow when it emits its own first event, and never before.",
    };
  };
}

/* ------------------------------------------------------------------------------------
 * views — residents
 * ---------------------------------------------------------------------------------- */

const RESIDENT_COLUMNS = "1.5fr .85fr .7fr 1.05fr 1.15fr .85fr";

async function viewResidents() {
  const [listing, ledgerRows] = await Promise.all([call("residents"), call("routines")]);
  const residents = listing.residents || [];
  const byResident = new Map();
  for (const row of ledgerRows.routines || []) {
    if (!byResident.has(row.resident)) byResident.set(row.resident, []);
    byResident.get(row.resident).push(row);
  }

  const out = frag(
    head("Residents",
      "Everything steward could validate under its residents tree. A manifest that did not " +
      "validate is named below rather than quietly left out — a fleet list that hides a broken " +
      "resident is worse than one that shows nothing."),
    (listing.errors || []).map((line) =>
      el("div", { class: "problem" },
        el("div", { class: "code" }, "manifest does not validate"),
        el("div", { class: "msg" }, line)))
  );

  if (!residents.length) {
    add(out, [rise(empty("No residents.",
      "Steward found no valid manifest under its residents tree. Either nothing is declared " +
      "yet — the New resident tab writes the first one — or the tree it was pointed at is not " +
      "the tree you think it is (--residents, or STEWARD_RESIDENTS)."), 1)]);
    return out;
  }

  const rows = el("div", { class: "rows" },
    el("div", { class: "row rowhead", style: { gridTemplateColumns: RESIDENT_COLUMNS } },
      el("span", {}, "resident"), el("span", {}, "runner"), el("span", {}, "skills"),
      el("span", {}, "routines"), el("span", {}, "budget"), el("span", {}, "takes work"))
  );

  residents.forEach((resident) => {
    const routines = byResident.get(resident.id) || [];
    const upcoming = routines
      .filter((row) => row.next_fire)
      .sort((a, b) => a.next_fire.localeCompare(b.next_fire))[0];
    const budget = resident.budget || {};
    add(rows, [el("a", {
      class: "row", href: `#/residents/${encodeURIComponent(resident.id)}`,
      style: { gridTemplateColumns: RESIDENT_COLUMNS, "--accent": resident.soul.accent },
    },
      el("span", { class: "who" },
        el("span", { class: "swatch", style: { background: resident.soul.accent } }),
        el("span", {},
          el("span", { class: "nm" }, resident.soul.name),
          " ",
          el("span", { class: "idd" }, resident.id),
          resident.retired ? " " : null,
          resident.retired ? badge("retired", "fail") : null,
          el("span", { class: "role" }, resident.soul.role))),
      el("span", { class: "stack" },
        resident.runner.kind,
        el("span", { class: "sub" }, resident.runner.model || "no model named")),
      el("span", { class: "stack" },
        `${resident.effective_skills.length} effective`,
        el("span", { class: "sub" }, `${resident.skills.length} granted`)),
      el("span", { class: "stack" },
        routines.length ? `${routines.length} declared` : el("span", { class: "faint" }, "none"),
        el("span", { class: "sub" },
          upcoming ? frag("next ", clock(upcoming.next_fire, "until")) : "nothing scheduled")),
      gauge(budget),
      el("span", { class: "badges" },
        // A retired resident's board and delegation blocks still say what they said; they
        // simply no longer decide anything, so rendering them "on" would be a lie the
        // manifest is technically telling. Retirement outranks every one of them.
        resident.retired
          ? el("span", { class: "faint", title: RETIRED_REFUSAL }, "nothing — retired")
          : frag(
              resident.board && resident.board.claim ? badge("board", "on") : null,
              resident.delegation && resident.delegation.send ? badge("delegates", "on") : null,
              inboxBadge(resident.routes),
              !(resident.board && resident.board.claim) &&
              !(resident.delegation && resident.delegation.send)
                ? el("span", { class: "faint" }, "routines only") : null))
    )]);
  });

  add(out, [rise(rows, 1)]);
  return out;
}

/** The inbox badge, read off route *status* — a shut door must not read as an open one.
 *
 * A delegation route flipped to `pending` or `disabled` still stops pickup, so badging it
 * "inbox on" would say a resident takes post that nothing will ever collect. */
function inboxBadge(routes) {
  const doors = (routes || []).filter((route) => route.kind === "delegation");
  if (!doors.length) return null;
  return doors.some((route) => route.status === "active")
    ? badge("inbox", "on")
    : badge("inbox closed", "fail");
}

function gauge(budget) {
  if (!budget || budget.declared === false) {
    return el("span", { class: "stack" },
      el("span", { class: "faint" }, budget && budget.summary ? budget.summary : "no limit"),
      el("span", { class: "sub" }, `${(budget && budget.runs) || 0} runs counted`));
  }
  const worst = (budget.budgets || [])
    .filter((item) => item.limit)
    .map((item) => item.spent / item.limit)
    .reduce((a, b) => Math.max(a, b), 0);
  const cls = budget.paused || worst >= 1 ? "hot" : worst >= 0.7 ? "warm" : "";
  return el("span", { class: "gauge" },
    el("span", { class: "track" },
      el("span", { class: "fill " + cls, style: { right: `${Math.max(0, 100 - worst * 100)}%` } })),
    el("span", { class: "cap" },
      budget.paused ? badge("paused", "fail") : null, budget.paused ? " " : null,
      budget.summary));
}

/* -- one resident -------------------------------------------------------------------- */

async function viewResident(route) {
  const id = route.id;
  const [resident, journal, inbox, budget, ledgerRows] = await Promise.allSettled([
    call("resident", { params: { resident_id: id } }),
    call("journal", { params: { resident_id: id }, query: { limit: 8 } }),
    call("inbox", { params: { resident_id: id } }),
    call("budget", { params: { resident_id: id } }),
    call("routines"),
  ]);

  if (resident.status === "rejected") {
    if (resident.reason === REPROMPT) throw REPROMPT;
    return frag(
      el("div", { class: "detailhead rise" },
        el("a", { class: "back", href: "#/residents" }, "← all residents"),
        el("h1", {}, id)),
      problem(resident.reason));
  }

  const it = resident.value;
  const soul = it.soul;
  // The fleet ledger knows the next fire and the anchor. If it did not answer, fall back
  // to the manifest alone and say nothing about either rather than guessing at them.
  const routines = ledgerRows.status === "fulfilled"
    ? (ledgerRows.value.routines || []).filter((row) => row.resident === it.id)
    : (it.routines || []).map((row) => ({
        ...row, key: `${it.id}/${row.id}`, resident: it.id, routine: row.id,
        retired: it.retired, next_fire: null, anchor: null, last_request: null,
      }));

  const out = frag(
    rise(el("div", { class: "detailhead", style: { "--accent": soul.accent } },
      el("a", { class: "back", href: "#/residents" }, "← all residents"),
      el("h1", {}, soul.name, it.retired ? " " : null,
        it.retired ? badge("retired", "fail") : null),
      el("div", { class: "under" },
        `${soul.role} · ${it.id}`,
        it.agent_id ? ` · ${it.agent_id}` : null,
        it.project ? ` · project ${it.project}` : null,
        it.retired ? " · retired: takes no routines, no board work, no letters" : null)), 0),

    rise(el("div", { class: "grid2" }, soulPanel(it), charterPanel(it.charter)), 1),
    rise(skillsPanel(it), 2),
    rise(routinesPanel(it, routines), 3),
    rise(budgetPanel(budget), 4),
    rise(journalPanel(journal), 5),
    rise(inboxPanel(inbox), 6)
  );
  return out;
}

function soulPanel(it) {
  return el("div", { class: "panel" },
    el("h3", {}, "Soul"),
    facts([
      ["name", soulSwatch(it.soul)],
      ["char", it.soul.char],
      ["role", it.soul.role],
      ["accent", it.soul.accent],
      ["summary", it.summary],
      ["manifest", mono(it.path)],
      ["memory", mono(`${it.memory.kind}: ${it.memory.path}`)],
      ["runner", `${it.runner.kind}${it.runner.model ? ` · ${it.runner.model}` : ""}`],
    ]),
    el("h3", { style: { marginTop: "20px" } }, "Voice"),
    it.voice
      ? el("blockquote", { class: "voice" }, it.voice)
      : el("p", { class: "faint", style: { margin: 0 } },
          "This soul declares no ## Voice section, so sessions for it get none. " +
          "Steward injects what the file says and never writes a voice on a resident's behalf.")
  );
}

function soulSwatch(soul) {
  return el("span", { class: "who" },
    el("span", { class: "swatch", style: { background: soul.accent } }),
    el("span", {}, soul.name));
}

function charterPanel(charter) {
  const escalation = charter.escalation;
  return el("div", { class: "panel" },
    el("h3", {}, "Charter"),
    el("p", { style: { marginTop: 0, lineHeight: 1.7 } }, charter.mission),
    el("div", { class: "label", style: { marginTop: "16px" } }, "duties"),
    el("ul", { class: "list" }, (charter.duties || []).map((duty) => el("li", {}, duty))),
    el("div", { class: "label", style: { marginTop: "16px" } }, "hard rules"),
    el("ul", { class: "list" }, (charter.rules || []).map((rule) => el("li", {}, rule))),
    el("div", { class: "label", style: { marginTop: "16px" } }, "escalation"),
    typeof escalation === "string"
      ? el("p", { style: { margin: "6px 0 0" } }, escalation)
      : frag(
          el("ul", { class: "list", style: { marginTop: "6px" } },
            (escalation.when || []).map((when) => el("li", {}, when))),
          el("p", { class: "dim", style: { margin: "6px 0 0" } },
            `raised as ${escalation.how}${escalation.note ? ` — ${escalation.note}` : ""}`))
  );
}

function skillsPanel(it) {
  const granted = new Set((it.skills || []).map((skill) => skill.id));
  const notes = new Map((it.skills || []).map((skill) => [skill.id, skill.note]));
  return el("div", { class: "panel" },
    el("h3", {}, "Effective skills"),
    el("p", { class: "dim", style: { marginTop: 0 } },
      "What a session for this resident is actually given: the library's defaults plus this " +
      "manifest's own grants, in injection order."),
    it.effective_skills.length
      ? el("div", {}, it.effective_skills.map((name) =>
          el("span", {
            class: "tag" + (granted.has(name) ? "" : " default"),
            title: granted.has(name)
              ? (notes.get(name) || "granted by this manifest")
              : "a library default — every resident holds it without a grant",
          }, name)))
      : el("p", { class: "faint", style: { margin: 0 } },
          "None. Either the library is empty or steward was pointed at no library at all, " +
          "in which case no grant is checked and no skill is injected.")
  );
}

function routinesPanel(it, routines) {
  const panel = el("div", { class: "panel" },
    el("h3", {}, "Routines"));
  if (!routines.length && !(it.routines || []).length) {
    add(panel, [empty("No standing work.",
      "This resident declares no routines, so it only ever wakes for work handed to it — " +
      "a job it claims from the board, or something delegated into its inbox.")]);
    return panel;
  }
  const rows = el("div", { class: "rows" },
    el("div", { class: "row rowhead", style: { gridTemplateColumns: "1fr 1.2fr 1fr 1fr auto" } },
      el("span", {}, "routine"), el("span", {}, "schedule"), el("span", {}, "next"),
      el("span", {}, "last asked-for run"), el("span", {}, "")));
  for (const row of routines) add(rows, [routineRow(row)]);
  add(panel, [rows]);
  return panel;
}

function routineRow(row) {
  return el("div", { class: "row hoverable", style: { gridTemplateColumns: "1fr 1.2fr 1fr 1fr auto" } },
    el("span", { class: "stack" }, row.routine,
      el("span", { class: "sub" },
        row.enabled ? `timeout ${row.timeout_s}s` : "disabled in the manifest",
        row.journal ? " · closes the day" : "")),
    el("span", { class: "stack" }, mono(row.schedule),
      el("span", { class: "sub" }, row.schedule_tz)),
    el("span", { class: "stack" },
      row.retired
        ? el("span", { class: "faint" }, "never")
        : row.enabled ? clock(row.next_fire, "until") : el("span", { class: "faint" }, "—"),
      el("span", { class: "sub" },
        row.anchor ? frag("anchored ", clock(row.anchor, "ago")) : "never fired")),
    lastRun(row.last_request),
    runButton(row));
}

function lastRun(request) {
  if (!request) {
    return el("span", { class: "faint" }, "none through this API");
  }
  const outcome = request.outcome || "";
  const kind = outcome === "ran" ? "live" : outcome === "queued" ? "wait" : "fail";
  return el("span", { class: "stack" },
    badge(outcome, kind),
    el("span", { class: "sub" }, clock(request.received_at, "ago")));
}

/* What steward answers a run-now for a retired resident: 409 resident_retired. The button
 * says it here rather than sending the request and rendering the refusal, because a
 * control that can only ever fail should look like one before it is pressed. */
const RETIRED_REFUSAL =
  "This resident is retired — its manifest declares retired: true — so steward answers " +
  "409 resident_retired to a run-now, fires no routine and claims no board work. Set " +
  "retired: false and commit that decision to bring it back.";

function runButton(row) {
  const button = el("button", { class: "ghost tiny", type: "button" }, "run now");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "asking…";
    try {
      const answer = await call("runRoutine", {
        method: "POST",
        params: { resident_id: row.resident, routine_id: row.routine },
      });
      ticket({
        what: `run ${row.key}`,
        requestId: answer.request_id,
        why: "accepted, not yet confirmed — steward has queued one run and will record what " +
             "it came to. " + answer.message,
        confirm: confirmRun(answer.request_id),
      });
    } catch (error) {
      if (error !== REPROMPT) {
        ticket({
          what: `run ${row.key}`,
          refused: true,
          why: error instanceof ApiError ? `${error.code} — ${error.message}` : String(error),
        });
      }
    } finally {
      button.disabled = !row.enabled || Boolean(row.retired);
      button.textContent = "run now";
    }
  });
  if (row.retired) {
    button.disabled = true;
    button.title = RETIRED_REFUSAL;
  } else if (!row.enabled) {
    button.disabled = true;
    button.title = "This routine is disabled in the manifest. Enable it there rather than " +
      "firing something the declaration says is off.";
  }
  return button;
}

function budgetPanel(settled) {
  const panel = el("div", { class: "panel" }, el("h3", {}, "Budget"));
  if (settled.status === "rejected") { add(panel, [problem(settled.reason)]); return panel; }
  const budget = settled.value;
  const spent = budget.spent || {};
  add(panel, [
    el("p", { class: "dim", style: { marginTop: 0 } },
      `Counted in ${budget.window.tz}, for the ${budget.window.day} window that runs to ` +
      `${stamp(budget.window.end)}. Every number is a sum over rows steward wrote when a run ` +
      `finished — nothing here is projected.`),
    budget.paused
      ? el("div", { class: "problem" },
          el("div", { class: "code" }, "paused"),
          el("div", { class: "msg" },
            `${budget.pause.reason}. Scheduled fires and board claims are skipped while this ` +
            `stands. Answer approval ${budget.pause.request_id} to carry on until the window ends.`))
      : null,
    (budget.budgets || []).length
      ? el("div", { class: "rows" },
          el("div", { class: "row rowhead", style: { gridTemplateColumns: "1fr 1fr 1fr 1fr" } },
            el("span", {}, "budget"), el("span", {}, "spent"),
            el("span", {}, "limit"), el("span", {}, "remaining")),
          (budget.budgets || []).map((item) =>
            el("div", { class: "row", style: { gridTemplateColumns: "1fr 1fr 1fr 1fr" } },
              el("span", {}, item.budget),
              el("span", { class: item.exhausted ? "" : "dim" }, String(item.spent)),
              el("span", { class: "dim" }, item.limit === null ? "no cap declared" : String(item.limit)),
              el("span", { class: "dim" }, item.remaining === null ? "—" : String(item.remaining)))))
      : empty("No caps declared.",
          "This resident has no budget block, so nothing stops it but its own schedule. " +
          "That is unlimited, not unknown."),
    facts([
      ["runs", `${spent.runs || 0}${spent.unreported_runs ? ` (${spent.unreported_runs} reported no usage)` : ""}`],
      ["tokens", String(spent.tokens || 0)],
      ["cost", `$${(spent.cost_usd || 0).toFixed(4)}`],
      ["seconds", String(Math.round(spent.duration_s || 0))],
      ["max run", budget.max_run_seconds ? `${budget.max_run_seconds}s` : null],
    ]),
  ]);
  return panel;
}

function journalPanel(settled) {
  const panel = el("div", { class: "panel" }, el("h3", {}, "Journal"));
  if (settled.status === "rejected") { add(panel, [problem(settled.reason)]); return panel; }
  const entries = settled.value.entries || [];
  if (!entries.length) {
    add(panel, [empty("Nothing written yet.",
      "The resident writes this, not steward — an entry appears when its closing routine runs " +
      "and the session actually writes one. No entry is invented on a resident's behalf, so an " +
      "empty journal means a day that was never closed.")]);
    return panel;
  }
  for (const entry of entries) {
    add(panel, [el("div", { class: "entry" },
      el("div", { class: "when" },
        entry.date, entry.routine ? ` · ${entry.routine}` : " · no routine named"),
      el("div", { class: "body" }, entry.text))]);
  }
  return panel;
}

/** One line naming which doors are open and how much post is sitting behind them.
 *
 * A closed route is named as closed rather than omitted: letters delivered before somebody
 * shut it stay open and nothing picks them up, and a panel listing only accepting routes
 * would show no route at all and leave the pile unexplained. */
function routeLine(routes, pending) {
  if (!routes.length) {
    return "This resident declares no delegation route, so nothing can be handed to it.";
  }
  const open = routes.filter((route) => route.accepts).map((route) => route.id);
  if (open.length) return `Open routes: ${open.join(", ")}. ${pending} waiting.`;
  const shut = routes.map((route) => `${route.id} (${route.status})`).join(", ");
  return pending
    ? `Every declared route is closed — ${shut}. ${pending} waiting behind it, ` +
      "and nothing will pick them up."
    : `Every declared route is closed — ${shut}. Nothing can be handed to this resident.`;
}

function inboxPanel(settled) {
  const panel = el("div", { class: "panel" }, el("h3", {}, "Inbox — delegated and waiting"));
  if (settled.status === "rejected") { add(panel, [problem(settled.reason)]); return panel; }
  const items = settled.value.inbox || [];
  add(panel, [el("p", { class: "dim", style: { marginTop: 0 } },
    routeLine(settled.value.routes || [], settled.value.pending || 0))]);
  if (!items.length) {
    add(panel, [empty("Empty inbox.",
      "Nothing is waiting. Work handed to this resident lands here and is picked up on its " +
      "own next wake-up; steward never prompts a resident to check.")]);
    return panel;
  }
  for (const item of items) {
    add(panel, [el("div", { class: "entry" },
      el("div", { class: "when" },
        `${item.status} · from ${item.delegated_by || "a person"} via ${item.route} · depth ${item.depth}`),
      el("div", { style: { marginTop: "5px" } }, item.title),
      item.detail ? el("div", { class: "dim", style: { marginTop: "4px" } }, item.detail) : null)]);
  }
  return panel;
}

/* ------------------------------------------------------------------------------------
 * views — new resident
 * ---------------------------------------------------------------------------------- */

const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
const ACCENT_PATTERN = /^#[0-9a-fA-F]{6}$/;

/** True when this install lets the form ask for a deploy. See index.html. */
function deployOffered() {
  return Boolean(window.STEWARD_UI && window.STEWARD_UI.deploy);
}

async function viewNew() {
  const library = await call("skills");
  const skills = library.skills || [];

  const errors = el("div", {});
  const result = el("div", {});
  const fields = {};

  function field(name, title, hint, node) {
    const err = el("p", { class: "err", id: `err-${name}` });
    fields[name] = { input: node, error: err };
    return el("label", { class: "field" },
      label(title), node, hint ? el("span", { class: "hint" }, hint) : null, err);
  }

  const idInput = el("input", { type: "text", name: "id", placeholder: "note-keeper", required: true });
  const accentInput = el("input", { type: "text", name: "accent", value: "#a68a4f" });
  const accentChip = el("span", { class: "chip", style: { background: "#a68a4f" } });
  accentInput.addEventListener("input", () => {
    accentChip.style.background = ACCENT_PATTERN.test(accentInput.value.trim())
      ? accentInput.value.trim() : "transparent";
  });

  const kindSelect = el("select", { name: "kind" },
    ...["claude", "codex", "command", "mock"].map((kind) =>
      el("option", { value: kind }, kind)));

  const form = el("form", { class: "panel", novalidate: true },
    el("h3", {}, "The declaration"),

    el("div", { class: "rowfields" },
      field("id", "id", "Directory under residents/. Lowercase letters, digits and hyphens; " +
        "must start with a letter or digit.", idInput),
      field("name", "name", "What burrow calls it. Hob, Quill, Maren.",
        el("input", { type: "text", name: "name", placeholder: "Quill" }))),

    el("div", { class: "rowfields" },
      field("char", "char", "The burrow sprite key.",
        el("input", { type: "text", name: "char", placeholder: "Scribe" })),
      field("role", "role", "One line, lowercase. Shown under the name.",
        el("input", { type: "text", name: "role", placeholder: "note bot" }))),

    field("accent", "accent", "Hex, #rrggbb. Burrow draws the villager in this colour.",
      el("span", { class: "swatchrow" }, accentChip, accentInput)),

    el("div", { class: "rowfields" },
      field("runnerKind", "runner kind", "Which brain every session for this resident launches on.",
        kindSelect),
      field("runnerModel", "runner model", "Passed to the CLI. Leave blank for that runner's default.",
        el("input", { type: "text", name: "model", placeholder: "claude-opus-5" }))),

    el("div", { class: "rowfields" },
      field("agentId", "agent_id", "Burrow identity, <source>:<name>. Leave blank and steward " +
        "derives one from the runner kind and the id.",
        el("input", { type: "text", name: "agent_id", placeholder: "derived" })),
      field("summary", "summary", "One line burrow can display. Optional.",
        el("input", { type: "text", name: "summary" }))),

    el("h3", { style: { marginTop: "26px" } }, "Charter"),
    field("mission", "mission", "One paragraph of purpose. Injected into every session.",
      el("textarea", { name: "mission" })),
    field("duties", "duties", "Standing responsibilities, one per line. At least one.",
      el("textarea", { name: "duties" })),
    field("rules", "rules", "Hard constraints, one per line. At least one.",
      el("textarea", { name: "rules" })),
    field("escalation", "escalation", "When to stop and ask instead of acting.",
      el("input", { type: "text", name: "escalation",
        placeholder: "Raise needs_human before anything irreversible." })),

    el("h3", { style: { marginTop: "26px" } }, "Soul body"),
    field("soulBody", "opening paragraph", "Who this resident actually is. Leave blank and " +
      "steward writes a skeleton that says out loud it is one.",
      el("textarea", { name: "soul_body" })),
    field("voice", "## voice", "Style guidance only — it changes nothing a resident may do. " +
      "Blank means no voice section at all, which is a real answer.",
      el("textarea", { name: "voice" })),

    el("h3", { style: { marginTop: "26px" } }, "Skills"),
    el("p", { class: "dim", style: { marginTop: "-6px" } },
      "A grant is what this resident holds on top of the library's defaults. Defaults are " +
      "shown but cannot be unticked: every resident holds them."),
    skillChecks(skills),

    deployOffered()
      ? frag(
          el("h3", { style: { marginTop: "26px" } }, "Deploy"),
          el("label", { class: "check", style: { marginTop: "4px" } },
            el("input", { type: "checkbox", name: "deploy" }),
            el("span", {}, el("span", { class: "nm" }, "deploy after declaring"),
              el("span", { class: "ds" },
                "Runs the whole nursery: writes the files, packs a compose bundle, pipes it " +
                "over ssh to the host this manifest addresses, brings the container up, and " +
                "checks the schedule. Left unticked, this form declares and stops."))))
      : null,

    el("hr", { class: "rule" }),
    errors,
    el("div", { class: "actions" },
      el("button", { class: "primary", type: "submit" }, "Declare resident"),
      el("span", { class: "note" },
        deployOffered()
          ? "Writes two files for review. Deploys only if you tick the box above — and " +
            "never commits: that is still yours."
          : "Writes two files for review. Deploys nothing, schedules nothing, emits nothing."))
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errors.replaceChildren();
    result.replaceChildren();
    const body = readForm(form, fields, skills);
    if (!body) {
      add(errors, [el("div", { class: "problem" },
        el("div", { class: "code" }, "not sent"),
        el("div", { class: "msg" },
          "Some fields do not match what steward's validator accepts. They are marked above; " +
          "nothing has been sent."))]);
      return;
    }
    try {
      const answer = await call("residents", { method: "POST", body });
      ticket({
        what: `${body.deploy ? "raise" : "declare"} ${answer.id}`,
        requestId: answer.request_id,
        why: answer.message,
        confirm: confirmDeclared(answer),
      });
      add(result, [declared(answer)]);
      form.reset();
      accentChip.style.background = "transparent";
      result.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      if (error !== REPROMPT) add(errors, [problem(error)]);
    }
  });

  return frag(
    head("New resident",
      "This writes ", el("strong", {}, "residents/<id>/manifest.yaml"), " and ",
      el("strong", {}, "soul.md"), ", reads them straight back through the ordinary validator, " +
      "and stops", deployOffered() ? " — unless you tick deploy, which hands the same " +
        "declaration to the nursery and provisions it on the host the manifest names" : "",
      ". Steward commits neither way, and no event is emitted on the new resident's behalf — " +
      "a villager appears in burrow when it genuinely exists and emits."),
    rise(result, 1),
    rise(form, 1)
  );
}

function skillChecks(skills) {
  if (!skills.length) {
    return empty("The library is empty.",
      "Steward found no skills to grant — either there is no skills/ directory beside the " +
      "residents tree, or nothing in it parses. A resident can still be declared; it will " +
      "simply hold nothing.");
  }
  return el("div", { class: "checks" }, skills.map((skill) =>
    el("label", { class: "check" },
      el("input", {
        type: "checkbox", name: "skill", value: skill.name,
        checked: skill.default, disabled: skill.default,
      }),
      el("span", {},
        el("span", { class: "nm" }, skill.name,
          skill.default ? " " : null,
          skill.default ? badge("default", "ember") : null),
        el("span", { class: "ds" }, skill.description)))));
}

/** Validate exactly what the server validates, and refuse to send anything else. */
function readForm(form, fields, skills) {
  const data = new FormData(form);
  const value = (name) => String(data.get(name) || "").trim();
  const lines = (name) => value(name).split("\n").map((line) => line.trim()).filter(Boolean);

  const complaints = [];
  const complain = (name, message) => {
    complaints.push(name);
    if (fields[name]) {
      fields[name].error.textContent = message;
      const input = fields[name].input.querySelector
        ? fields[name].input.querySelector("input, textarea, select") || fields[name].input
        : fields[name].input;
      if (input.setAttribute) input.setAttribute("aria-invalid", "true");
    }
  };
  for (const entry of Object.values(fields)) {
    entry.error.textContent = "";
    if (entry.input.removeAttribute) entry.input.removeAttribute("aria-invalid");
  }

  if (!ID_PATTERN.test(value("id"))) {
    complain("id", "Lowercase letters, digits and hyphens only, starting with a letter or digit " +
      "— the pattern the manifest schema enforces: ^[a-z0-9][a-z0-9-]*$");
  }
  if (!ACCENT_PATTERN.test(value("accent"))) complain("accent", "Six hex digits after a #, e.g. #4f7ea6.");
  if (!value("name")) complain("name", "Required: burrow draws this.");
  if (!value("char")) complain("char", "Required: burrow needs a sprite key.");
  if (!value("role")) complain("role", "Required: one line, shown under the name.");
  if (!value("mission")) complain("mission", "Required: one paragraph of purpose.");
  if (!lines("duties").length) complain("duties", "At least one duty, one per line.");
  if (!lines("rules").length) complain("rules", "At least one hard rule, one per line.");
  if (!value("escalation")) complain("escalation", "Required: when this resident stops and asks.");
  if (complaints.length) return null;

  const defaults = new Set(skills.filter((skill) => skill.default).map((skill) => skill.name));
  const granted = data.getAll("skill").map(String).filter((name) => !defaults.has(name));

  const body = {
    id: value("id"),
    name: value("name"),
    char: value("char"),
    accent: value("accent"),
    role: value("role"),
    charter: {
      mission: value("mission"),
      duties: lines("duties"),
      rules: lines("rules"),
      escalation: value("escalation"),
    },
    runner: { kind: value("kind") || "claude" },
  };
  if (value("model")) body.runner.model = value("model");
  if (value("agent_id")) body.agent_id = value("agent_id");
  if (value("summary")) body.summary = value("summary");
  if (value("soul_body")) body.soul_body = value("soul_body");
  if (value("voice")) body.voice = value("voice");
  if (granted.length) body.skills = granted;
  // Sent as a boolean whenever the switch put a checkbox on the form, ticked or not:
  // `deploy: false` is something this console means to say. With the switch off there is
  // no checkbox and no key, and the endpoint's own default — declare, deploy nothing —
  // is what stands. Asking a machine to start a container is never a field left out.
  if (form.elements.deploy) body.deploy = form.elements.deploy.checked;
  return body;
}

/* The answer to a POST /residents, rendered. Every line below is a field steward sent
 * back — the nursery reports its own stages, and this panel prints them rather than
 * describing what a deploy usually does. */
function declared(answer) {
  const panel = el("div", { class: "panel", style: { borderLeft: "3px solid var(--ember)" } },
    el("h3", {}, answer.provision ? "Raised" : "Declared — and that is all"),
    facts([
      ["id", answer.id],
      ["manifest", mono(answer.manifest_path)],
      ["soul", mono(answer.soul_path)],
      ["request", mono(answer.request_id)],
      ["declare", answer.declare
        ? `${answer.declare.written ? "written" : "already there"} · ${answer.declare.note}`
        : null],
      ["committed", answer.declare
        ? (answer.declare.commit || "no — the server does not commit; that is still yours")
        : null],
    ]));

  add(panel, (answer.warnings || []).map((line) =>
    el("div", { class: "problem" },
      el("div", { class: "code" }, "warning"),
      el("div", { class: "msg" }, line))));

  if (answer.provision) add(panel, [provisionBlock(answer.provision)]);
  if (answer.register) add(panel, [registerBlock(answer.register)]);

  add(panel, [el("p", { style: { marginBottom: 0, lineHeight: 1.75 } },
    "Both files were read back through ", el("strong", {}, "steward validate"),
    " before this answer, so what is on disk is something steward accepts. ",
    answer.provision
      ? "Commit the two files — the server did not — and the resident appears in burrow " +
        "when it emits its own first event, not because anything here says it exists."
      : "It is not a resident yet. Commit the two files, edit the soul body into somebody " +
        "real, then tick deploy here or run steward new-resident on a terminal.")]);
  return panel;
}

function provisionBlock(provision) {
  const target = provision.target || {};
  return frag(
    el("div", { class: "label", style: { marginTop: "18px" } }, "provision"),
    facts([
      ["host", `${target.user}@${target.host}:${target.path}`],
      ["container", target.container],
      ["image", target.image],
      ["sent", provision.sent
        ? "yes — the bundle was uploaded"
        : "no — the host already matched, byte for byte"],
      ["compose", provision.compose_changed === null
        ? "not compared: a dry run does not reach the host"
        : provision.compose_changed ? "re-rendered" : "unchanged"],
      ["files", (provision.files || []).join(", ")],
      [".env carries", (provision.env_keys || []).join(", ") + " (names only, never values)"],
    ]),
    el("div", { class: "label", style: { marginTop: "12px" } }, "commands steward ran"),
    el("ul", { class: "list" }, (provision.commands || []).map((line) => el("li", {}, mono(line)))),
    el("details", { style: { marginTop: "10px" } },
      el("summary", {}, "the compose fragment, verbatim"),
      el("pre", {}, provision.compose || ""))
  );
}

function registerBlock(register) {
  const fires = register.next_fires || [];
  return frag(
    el("div", { class: "label", style: { marginTop: "18px" } }, "register"),
    register.ok
      ? null
      : el("div", { class: "problem" },
          el("div", { class: "code" }, "the schedule check did not pass"),
          el("div", { class: "msg" }, (register.problems || []).join("; "))),
    fires.length
      ? el("ul", { class: "list" }, fires.map((fire) =>
          el("li", {}, `${fire.routine} fires next at `, mono(fire.at))))
      : el("p", { class: "faint", style: { margin: "6px 0 0" } },
          "No enabled routine, so this resident fires nothing on a schedule. There is no " +
          "second registry: a routine is scheduled because a manifest declares it.")
  );
}

/* ------------------------------------------------------------------------------------
 * views — routines
 * ---------------------------------------------------------------------------------- */

async function viewRoutines() {
  const data = await call("routines");
  const routines = data.routines || [];

  const scheduler = data.scheduler || null;

  const out = frag(
    head("Routines",
      "Every routine every valid resident declares, fleet-wide. An enabled routine is a " +
      "declaration, not a heartbeat — so the heartbeat is here too: ",
      schedulerBadge(scheduler), schedulerNote(scheduler),
      " The anchor is the moment the next occurrence is computed from: the last fire, or " +
      "when steward first saw the routine. A retired resident's routines are still listed " +
      "and never fire: they are what used to run here, which is a question a ledger should " +
      "be able to answer."),
    (data.errors || []).map((line) =>
      el("div", { class: "problem" },
        el("div", { class: "code" }, "manifest does not validate"),
        el("div", { class: "msg" }, line)))
  );

  if (!routines.length) {
    add(out, [rise(empty("No standing work anywhere.",
      "No valid resident declares a routine. Either the fleet only ever works on demand — " +
      "board claims and delegated inboxes — or the residents tree steward was pointed at is " +
      "empty."), 1)]);
    return out;
  }

  const columns = "1fr .9fr 1.1fr .95fr .95fr 1fr auto";
  const rows = el("div", { class: "rows" },
    el("div", { class: "row rowhead", style: { gridTemplateColumns: columns } },
      el("span", {}, "resident"), el("span", {}, "routine"), el("span", {}, "schedule"),
      el("span", {}, "next"), el("span", {}, "anchor"), el("span", {}, "last asked-for run"),
      el("span", {}, "")));

  for (const row of routines) {
    add(rows, [el("div", { class: "row hoverable", style: { gridTemplateColumns: columns } },
      el("a", { class: "who", href: `#/residents/${encodeURIComponent(row.resident)}`,
        style: { textDecoration: "none", color: "inherit" } },
        el("span", { class: "swatch", style: { background: row.accent } }),
        el("span", {},
          el("span", { class: "nm", style: { fontSize: "15px" } }, row.resident_name),
          el("span", { class: "role" }, row.resident))),
      el("span", { class: "stack" }, row.routine,
        el("span", { class: "sub" },
          row.enabled ? badge("enabled", "on") : badge("disabled", "fail"),
          row.retired ? " " : null,
          row.retired ? badge("retired", "fail") : null)),
      el("span", { class: "stack" }, mono(row.schedule),
        el("span", { class: "sub" }, row.schedule_tz)),
      row.retired
        ? el("span", { class: "faint", title: RETIRED_REFUSAL }, "retired — never fires")
        : row.enabled
          ? clock(row.next_fire, "until")
          : el("span", { class: "faint" }, "no next fire"),
      row.anchor ? clock(row.anchor, "ago") : el("span", { class: "faint" }, "never fired"),
      lastRun(row.last_request),
      runButton(row))]);
  }

  add(out, [rise(rows, 1),
    rise(el("p", { class: "faint", style: { marginTop: "18px", maxWidth: "78ch", lineHeight: 1.7 } },
      `Scheduler state read from ${data.state_path}. "Last asked-for run" is the request log: ` +
      "runs somebody asked for through this API. A routine that fires on its own schedule " +
      "leaves its record in burrow's event log, not here."), 2)]);
  return out;
}

/* ------------------------------------------------------------------------------------
 * views — approvals
 * ---------------------------------------------------------------------------------- */

async function viewApprovals() {
  const data = await call("approvals", { query: { status: "all" } });
  const all = data.approvals || [];
  const pending = all.filter((item) => item.status === "pending");
  const decided = all.filter((item) => item.status !== "pending").reverse();

  const out = frag(
    head("Approvals",
      "Gated actions waiting on a person. Steward never invents one of these: a request is " +
      "created by the session that reached the gate, and this console only ever answers it. " +
      "Decisions are recorded once — the first one wins, and a replay changes nothing."),
    section("Pending", pending.length)
  );

  add(out, [pending.length
    ? rise(el("div", {}, pending.map((item, index) => approvalCard(item, index))), 1)
    : rise(empty("Nothing is waiting on you.",
        "No session has reached a gated action, or every request has already been answered. " +
        "A request nobody answers before its expiry resolves itself as a denial, recorded " +
        "against \"expiry\" rather than against you."), 1)]);

  add(out, [section("Decided", decided.length)]);
  if (!decided.length) {
    add(out, [rise(empty("Nothing decided yet.",
      "This is the audit view — request and decision in one row. It fills up as approvals " +
      "are answered here, from the CLI, or by expiry."), 3)]);
    return out;
  }

  const columns = "1.4fr .8fr .8fr 1fr 1fr";
  const rows = el("div", { class: "rows" },
    el("div", { class: "row rowhead", style: { gridTemplateColumns: columns } },
      el("span", {}, "action"), el("span", {}, "resident"), el("span", {}, "decision"),
      el("span", {}, "decided by"), el("span", {}, "when")));
  for (const item of decided) {
    add(rows, [el("div", { class: "row", style: { gridTemplateColumns: columns } },
      el("span", { class: "stack" }, item.action,
        el("span", { class: "sub" }, item.message)),
      el("span", { class: "dim" }, item.resident || item.agent_id),
      badge(item.decision || "—", item.decision === "approve" ? "live" : "fail"),
      el("span", { class: "dim" }, item.decided_by || "—"),
      clock(item.decided_at, "ago"))]);
  }
  add(out, [rise(rows, 3)]);
  return out;
}

function approvalCard(item, index) {
  const errors = el("div", {});
  const editor = el("textarea", { rows: 8, style: { display: "none" } },
    JSON.stringify(item.detail || {}, null, 2));
  const editorError = el("p", { class: "err" });

  const send = async (decision, edit) => {
    errors.replaceChildren();
    try {
      const answer = await call("approval", {
        method: "POST",
        params: { request_id: item.request_id },
        body: edit === undefined ? { decision } : { decision, edit },
      });
      ticket({
        what: `${decision} ${item.action}`,
        requestId: item.request_id,
        why: answer.message,
        confirm: confirmApproval(item.request_id),
      });
    } catch (error) {
      if (error !== REPROMPT) add(errors, [problem(error)]);
    }
  };

  const editButton = el("button", { class: "ghost", type: "button" }, "Edit…");
  let editing = false;
  editButton.addEventListener("click", () => {
    if (!editing) {
      editing = true;
      editor.style.display = "block";
      editButton.textContent = "Send edit";
      editor.focus();
      return;
    }
    editorError.textContent = "";
    let parsed;
    try {
      parsed = JSON.parse(editor.value);
    } catch (error) {
      editorError.textContent = `That is not JSON: ${error.message}. Nothing was sent.`;
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      editorError.textContent =
        "The edit must be a JSON object — steward records it as the modified detail. " +
        "Nothing was sent.";
      return;
    }
    send("edit", parsed);
  });

  const card = el("div", { class: "panel" },
    el("div", { style: { display: "flex", justifyContent: "space-between",
      gap: "16px", flexWrap: "wrap", alignItems: "baseline" } },
      el("div", {},
        el("div", { class: "label" }, item.action),
        el("div", { class: "serif", style: { fontSize: "19px", marginTop: "6px" } }, item.message)),
      el("div", { class: "badges" },
        badge(item.resident || item.agent_id, "on"),
        item.expires_at
          ? el("span", { class: "badge wait" }, el("span", { class: "sq" }),
              frag("expires ", clock(item.expires_at, "until")))
          : badge("no expiry", ""))),

    el("p", { class: "faint", style: { marginTop: "12px", marginBottom: 0 } },
      frag("raised ", clock(item.created_at, "ago"), ` · ${item.request_id}`)),

    Object.keys(item.detail || {}).length
      ? el("details", { style: { marginTop: "14px" }, open: index === 0 },
          el("summary", { class: "label", style: { cursor: "pointer" } }, "detail"),
          jsonBlock(item.detail))
      : el("p", { class: "faint", style: { marginTop: "14px", marginBottom: 0 } },
          "The request carries no structured detail."),

    el("div", {}, editor, editorError),
    errors,
    el("div", { class: "actions", style: { marginTop: "16px" } },
      el("button", { class: "primary", type: "button",
        on: { click: () => send("approve") } }, "Approve"),
      el("button", { class: "danger", type: "button",
        on: { click: () => send("deny") } }, "Deny"),
      editButton,
      el("span", { class: "note" },
        (item.options || []).length ? `options: ${item.options.join(", ")}` : null))
  );

  return rise(card, index + 1);
}

/* ------------------------------------------------------------------------------------
 * views — job board
 * ---------------------------------------------------------------------------------- */

const BOARD_ORDER = [
  ["open", "Open", "Posted and unclaimed. A resident claims one on its own next wake-up; " +
    "steward prompts nobody."],
  ["claimed", "Claimed", "Held under a lease. If the lease expires before the claimant " +
    "finishes, the task fails and returns."],
  ["done", "Done", "The claimant said so and named its artifacts."],
  ["failed", "Failed", "The claimant gave up, or its lease ran out."],
];

async function viewBoard() {
  const [board, library, listing] = await Promise.all([
    call("jobs"), call("skills"), call("residents"),
  ]);
  const jobs = board.jobs || [];
  const names = new Map((listing.residents || []).map((item) => [item.id, item.soul]));

  const out = frag(
    head("Job board",
      "Work nobody has been told to do yet. Posting puts a task in steward's store and " +
      "announces it; dispatch is pull-based, so no resident is prompted and ",
      el("strong", {}, "task_claimed"),
      " in burrow's log is the only proof one picked it up."),
    rise(postForm(library.skills || []), 1)
  );

  BOARD_ORDER.forEach(([status, title, why], index) => {
    const group = jobs.filter((job) => job.status === status);
    add(out, [section(title, group.length)]);
    add(out, [rise(group.length
      ? el("div", { class: "rows" }, group.map((job) => jobRow(job, names)))
      : empty(`No ${status} tasks.`, why), index + 2)]);
  });
  return out;
}

function postForm(skills) {
  const errors = el("div", {});
  const title = el("input", { type: "text", name: "title", placeholder: "Research X" });
  const titleError = el("p", { class: "err" });
  const detail = el("textarea", { name: "detail",
    placeholder: "Everything the claimant needs to know." });

  const form = el("form", { class: "panel", novalidate: true },
    el("h3", {}, "Post a task"),
    el("label", { class: "field" }, label("title"), title,
      el("span", { class: "hint" }, "One line naming the work."), titleError),
    el("label", { class: "field" }, label("detail"), detail),
    el("div", { class: "field" }, label("required skills"),
      el("span", { class: "hint" },
        "A resident may only claim this if its effective skills cover every one of these."),
      skills.length
        ? el("div", { class: "checks", style: { marginTop: "8px" } }, skills.map((skill) =>
            el("label", { class: "check" },
              el("input", { type: "checkbox", name: "required", value: skill.name }),
              el("span", {}, el("span", { class: "nm" }, skill.name),
                el("span", { class: "ds" }, skill.description)))))
        : el("p", { class: "faint" }, "The library is empty, so nothing can be required.")),
    errors,
    el("div", { class: "actions" },
      el("button", { class: "primary", type: "submit" }, "Post to the board"),
      el("span", { class: "note" }, "Announced as task_posted. Nobody is prompted."))
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errors.replaceChildren();
    titleError.textContent = "";
    const data = new FormData(form);
    const value = String(data.get("title") || "").trim();
    if (!value) {
      titleError.textContent = "A task needs a title. Nothing was sent.";
      return;
    }
    try {
      const answer = await call("jobs", {
        method: "POST",
        body: {
          title: value,
          detail: String(data.get("detail") || "").trim(),
          required_skills: data.getAll("required").map(String),
        },
      });
      ticket({
        what: `post "${value}"`,
        requestId: answer.request_id,
        why: answer.message,
        confirm: confirmJob(answer.task_id),
      });
      form.reset();
    } catch (error) {
      if (error !== REPROMPT) add(errors, [problem(error)]);
    }
  });
  return form;
}

function jobRow(job, names) {
  const columns = "1.6fr 1fr .9fr 1fr";
  const claimant = job.claimant || job.assignee;
  const soul = claimant ? names.get(claimant) : null;
  return el("div", { class: "row", style: { gridTemplateColumns: columns } },
    el("span", { class: "stack" },
      el("span", { class: "serif", style: { fontSize: "16px" } }, job.title),
      el("span", { class: "sub" }, job.detail || "no detail given")),
    el("span", {},
      (job.required_skills || []).length
        ? (job.required_skills || []).map((name) => tag(name))
        : el("span", { class: "faint" }, "no skills required")),
    claimant
      ? el("a", { class: "who", href: `#/residents/${encodeURIComponent(claimant)}`,
          style: { textDecoration: "none", color: "inherit" } },
          soul ? el("span", { class: "swatch", style: { background: soul.accent } }) : null,
          el("span", {}, soul ? soul.name : claimant,
            el("span", { class: "role" }, job.assignee ? "delegated" : "claimed")))
      : el("span", { class: "faint" }, `posted by ${job.posted_by}`),
    el("span", { class: "stack" },
      clock(job.finished_at || job.claimed_at || job.created_at, "ago"),
      el("span", { class: "sub" },
        job.status === "claimed" && job.lease_expires_at
          ? frag("lease ", clock(job.lease_expires_at, "until"))
          : job.outcome || job.reason || `posted ${stamp(job.created_at)}`)));
}

/* ------------------------------------------------------------------------------------
 * views — skills
 * ---------------------------------------------------------------------------------- */

async function viewSkills() {
  const [library, listing] = await Promise.all([call("skills"), call("residents")]);
  const skills = library.skills || [];
  const residents = listing.residents || [];
  const names = new Map(residents.map((item) => [item.id, item.soul]));

  const out = frag(
    head("Skills",
      "The library steward materializes into a session's skills home. ",
      el("strong", {}, "Read-only"),
      ": a skill is added by committing a SKILL.md and granted by committing a manifest. " +
      "There is no HTTP path that writes either, and this console does not pretend otherwise."),
    (library.errors || []).map((line) =>
      el("div", { class: "problem" },
        el("div", { class: "code" }, "skill does not parse" ),
        el("div", { class: "msg" }, line)))
  );

  if (!skills.length) {
    add(out, [rise(empty("No library.",
      "Steward found no skills/ directory beside the residents tree, or nothing in it parses. " +
      "That is not an error — it means no grant is checked and no skill is injected."), 1)]);
    return out;
  }

  add(out, [rise(el("div", { class: "grid3" }, skills.map((skill) =>
    el("div", { class: "panel", style: { marginBottom: 0 } },
      el("div", { class: "badges", style: { marginBottom: "10px" } },
        skill.default ? badge("default", "ember") : badge("by grant", ""),
        badge(`${skill.holders.length} holder${skill.holders.length === 1 ? "" : "s"}`, "")),
      el("div", { class: "serif", style: { fontSize: "20px" } }, skill.name),
      el("p", { class: "dim", style: { lineHeight: 1.65 } }, skill.description),
      el("div", {}, skill.holders.length
        ? skill.holders.map((id) =>
            el("a", { class: "tag", href: `#/residents/${encodeURIComponent(id)}`,
              style: { textDecoration: "none" } }, (names.get(id) || {}).name || id))
        : el("span", { class: "faint" }, "Nobody holds this. That is a real answer, not an omission.")),
      el("p", { class: "faint", style: { marginBottom: 0, fontSize: "10.5px" } },
        `${skill.path || "no path"} · ${skill.body_chars} chars`)))), 1)]);

  add(out, [section("Who holds what", residents.length)]);
  const columns = "1fr 2.4fr";
  const rows = el("div", { class: "rows" },
    el("div", { class: "row rowhead", style: { gridTemplateColumns: columns } },
      el("span", {}, "resident"), el("span", {}, "effective skills — defaults marked")));
  for (const resident of residents) {
    const granted = new Set((resident.skills || []).map((skill) => skill.id));
    add(rows, [el("div", { class: "row", style: { gridTemplateColumns: columns } },
      el("a", { class: "who", href: `#/residents/${encodeURIComponent(resident.id)}`,
        style: { textDecoration: "none", color: "inherit" } },
        el("span", { class: "swatch", style: { background: resident.soul.accent } }),
        el("span", {}, el("span", { class: "nm", style: { fontSize: "15px" } }, resident.soul.name),
          el("span", { class: "role" }, resident.id))),
      el("span", {}, resident.effective_skills.map((name) =>
        tag(name, granted.has(name) ? "" : "default"))))]);
  }
  add(out, [rise(rows, 2),
    rise(el("p", { class: "faint", style: { marginTop: "16px", maxWidth: "78ch", lineHeight: 1.7 } },
      "Amber means a library default — held without a grant. To change what a resident holds, " +
      "edit the skills: block in residents/<id>/manifest.yaml and commit it. The repo is the " +
      "source of truth, and steward has no endpoint that would let this page pretend otherwise."), 3)]);
  return out;
}

/* ------------------------------------------------------------------------------------
 * routing
 * ---------------------------------------------------------------------------------- */

const VIEWS = {
  residents: viewResidents,
  resident: viewResident,
  new: viewNew,
  routines: viewRoutines,
  approvals: viewApprovals,
  board: viewBoard,
  skills: viewSkills,
};

function parseHash() {
  const raw = (window.location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  if (!parts.length) return { view: "residents", nav: "residents" };
  if (parts[0] === "residents" && parts[1]) {
    return { view: "resident", nav: "residents", id: parts[1] };
  }
  const view = VIEWS[parts[0]] ? parts[0] : "residents";
  return { view, nav: view };
}

const main = document.getElementById("main");
let renderToken = 0;

async function render() {
  const route = parseHash();
  const mine = ++renderToken;

  for (const anchor of document.querySelectorAll("#nav a")) {
    if (anchor.dataset.view === route.nav) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }

  try {
    const node = await VIEWS[route.view](route);
    if (mine !== renderToken) return;
    main.replaceChildren(node);
  } catch (error) {
    if (mine !== renderToken) return;
    // The gate has already been answered by the time this lands, so try the view again
    // with whatever the person just pasted. A wrong token simply asks once more.
    if (error === REPROMPT) return render();
    main.replaceChildren(frag(
      head("Something did not answer",
        "The console asked steward for this view and did not get it. The response is below, " +
        "verbatim — nothing has been retried or guessed at."),
      problem(error),
      el("button", { class: "ghost", type: "button", on: { click: () => render() } }, "Try again")));
  }
}

window.addEventListener("hashchange", render);

(async function start() {
  if (storedToken() === null) await askForToken(null);
  render();
})();
