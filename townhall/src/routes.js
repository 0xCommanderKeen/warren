/* Townhall's routing, as pure functions.
 *
 * The deployed build is mounted under a path prefix — `pnpm build --base=/observatory/`
 * on the NAS — and the old router read `window.location.pathname` raw, so under that
 * prefix every deep link resolved to a page that does not exist. Vite hands the prefix to
 * the bundle as `import.meta.env.BASE_URL`; everything here takes it as an argument
 * instead, so the tests can exercise both mounts without a build.
 *
 * A route is the app-facing path with no prefix: "/", "/skills/triage". A path is what
 * the address bar holds: "/observatory/skills/triage". `withBase` and `stripBase` are the
 * only two places that know the difference.
 */

/** Reduce a Vite base ("/", "/observatory/", "observatory") to "" or "/observatory". */
export function normalizeBase(base) {
  const trimmed = String(base ?? "").trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

/** The address-bar path for an app route. */
export function withBase(route, base) {
  const prefix = normalizeBase(base);
  const tail = String(route || "/");
  const path = `${prefix}${tail.startsWith("/") ? tail : `/${tail}`}`;
  return path === "" ? "/" : path;
}

/** The app route inside an address-bar path, or null when the path is not ours at all. */
export function stripBase(pathname, base) {
  const prefix = normalizeBase(base);
  const path = String(pathname || "/");
  if (!prefix) return path || "/";
  if (path === prefix) return "/";
  if (path.startsWith(`${prefix}/`)) return path.slice(prefix.length) || "/";
  return null;
}

const SEGMENTS = (route) =>
  String(route || "/")
    .split("?")[0]
    .split("/")
    .filter(Boolean)
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch {
        return part;
      }
    });

/**
 * Which page a route names, and the parameters it carries.
 *
 * An unknown route is its own answer (`page: "unknown"`) rather than a silent redirect to
 * the fleet: a deep link that has gone stale should say so, the way the console's own
 * 404 does.
 */
export function matchRoute(route) {
  const parts = SEGMENTS(route);
  if (!parts.length) return { page: "fleet", params: {} };

  if (parts[0] === "agents" && parts.length === 2) {
    return { page: "agent", params: { uuid: parts[1] } };
  }
  if (parts[0] === "residents") {
    if (parts.length === 1) return { page: "residents", params: {} };
    // Before the id branch, or a resident could be declared with the id "new" and take
    // this address away from the form that declares it.
    if (parts.length === 2 && parts[1] === "new") return { page: "residentNew", params: {} };
    if (parts.length === 2) return { page: "resident", params: { id: parts[1] } };
    if (parts.length === 3 && parts[2] === "declaration") {
      return { page: "residentDeclaration", params: { id: parts[1] } };
    }
  }
  if (parts[0] === "skills") {
    if (parts.length === 1) return { page: "skills", params: {} };
    if (parts.length === 2 && parts[1] === "new") return { page: "skillNew", params: {} };
    if (parts.length === 2) return { page: "skill", params: { name: parts[1] } };
  }
  if (parts[0] === "routines" && parts.length === 1) return { page: "routines", params: {} };
  if (parts[0] === "approvals" && parts.length === 1) return { page: "approvals", params: {} };
  if (parts[0] === "board" && parts.length === 1) return { page: "board", params: {} };
  if (parts[0] === "budgets") {
    if (parts.length === 1) return { page: "budgets", params: {} };
    if (parts.length === 2) return { page: "budgets", params: { id: parts[1] } };
  }
  return { page: "unknown", params: {} };
}

/** Route builders, so no page hand-writes a path and forgets to encode a name. */
export const routeTo = {
  fleet: () => "/",
  agent: (uuid) => `/agents/${encodeURIComponent(uuid)}`,
  residents: () => "/residents",
  // Addressed by uid rather than id, the way the console decided to (steward #112): an id
  // is a directory name, so retiring `pip` and raising a new one next month moves it, and
  // a bookmarked /residents/pip would quietly come to mean somebody else. The uid never
  // moves, and steward's routes resolve either spelling — ids first and exhaustively — so
  // a link written against an id keeps working.
  resident: (id) => `/residents/${encodeURIComponent(id)}`,
  residentNew: () => "/residents/new",
  residentDeclaration: (id) => `/residents/${encodeURIComponent(id)}/declaration`,
  skills: () => "/skills",
  skill: (name) => `/skills/${encodeURIComponent(name)}`,
  skillNew: () => "/skills/new",
  routines: () => "/routines",
  approvals: () => "/approvals",
  board: () => "/board",
  budgets: (id) => (id ? `/budgets/${encodeURIComponent(id)}` : "/budgets"),
};

/** The sidebar, in the console's order. `nav` is which entry a page lights up. */
export const NAV = [
  { index: "01", label: "Fleet", nav: "fleet", route: routeTo.fleet(), pages: ["fleet", "agent"] },
  { index: "02", label: "Residents", nav: "residents", route: routeTo.residents(), pages: ["residents", "resident", "residentNew", "residentDeclaration"] },
  { index: "03", label: "Routines", nav: "routines", route: routeTo.routines(), pages: ["routines"] },
  { index: "04", label: "Approvals", nav: "approvals", route: routeTo.approvals(), pages: ["approvals"] },
  { index: "05", label: "Board", nav: "board", route: routeTo.board(), pages: ["board"] },
  { index: "06", label: "Skills", nav: "skills", route: routeTo.skills(), pages: ["skills", "skill", "skillNew"] },
  { index: "07", label: "Budgets", nav: "budgets", route: routeTo.budgets(), pages: ["budgets"] },
];

/** Which sidebar entry owns a page, or null when none does (the 404). */
export function navFor(page) {
  return NAV.find((entry) => entry.pages.includes(page))?.nav ?? null;
}
