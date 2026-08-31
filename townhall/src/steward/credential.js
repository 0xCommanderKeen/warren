/* The operator credential, held client-side and nowhere else.
 *
 * Townhall's reads come from Chronicle's public `/state`; its writes go to steward, which
 * gates every route on a bearer credential. That credential is typed by the human at
 * runtime and kept in this tab's `sessionStorage` — never baked into the bundle, never a
 * session credential (steward answers 403 on every write to one of those, by design).
 *
 * **What to paste is an operator credential** — `steward operator mint <name>`, warren#225.
 * The master `STEWARD_TOKEN` still works, because it is the credential of steward's own
 * environment and CLI, but it is the wrong thing to put in a browser: it names nobody in
 * the audit trail, it is the same secret that boots the server, and revoking it means
 * restarting. An operator credential is revocable on the next request, and every write made
 * with one is committed under that person's name. The console this ports from could only
 * ever ask for the master token; that is the habit this module exists to end.
 *
 * This module is deliberately the only thing that knows *how* the credential is obtained
 * and carried. `createStewardClient` takes any object with `status()` and `headers()`, so
 * the next kind of credential — a cookie session, an OIDC exchange — swaps in here without
 * touching a single call site.
 */

//: What a minted operator credential looks like, so the box can say when you have pasted
//: something else. It is a hint and never a gate: steward decides, and a paste that does
//: not match is still sent, because refusing locally would break the master token and any
//: future credential shape this file has not been told about.
export const OPERATOR_PREFIX = "steward-operator-";

export const CREDENTIAL_KEY = "townhall.steward.operator";

/** No storage at all (a locked-down browser) must not take the console down with it. */
function safeStorage(storage) {
  try {
    if (!storage) return null;
    const probe = `${CREDENTIAL_KEY}.probe`;
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return storage;
  } catch {
    return null;
  }
}

/**
 * @param storage  where the token rests for the life of the tab. `null` keeps it in
 *                 memory only, which is what a browser refusing storage gets.
 */
export function createOperatorCredential({ storage } = {}) {
  const store = safeStorage(storage);
  let memory = null;
  const listeners = new Set();

  const read = () => (store ? store.getItem(CREDENTIAL_KEY) : memory);
  const announce = () => listeners.forEach((listener) => listener(status()));

  /**
   * "unknown" — nobody has said anything yet, so ask before writing.
   * "held"    — a credential is in hand.
   * "open"    — the human said out loud this steward runs open (`serve --allow-open`),
   *             stored as the empty string: known, and known to be nothing.
   */
  function status() {
    const value = read();
    if (value === null || value === undefined) return "unknown";
    return value === "" ? "open" : "held";
  }

  /**
   * Whether what is held looks like a *minted operator* credential rather than the master
   * token. Reported so the rail can say which one this tab is carrying — knowing that the
   * master token is loose in a browser is the point of the whole exercise.
   */
  function kind() {
    const value = read();
    if (!value) return null;
    return value.startsWith(OPERATOR_PREFIX) ? "operator" : "other";
  }

  function headers() {
    const value = read();
    if (value === null || value === undefined) return null;
    return value === "" ? {} : { Authorization: `Bearer ${value}` };
  }

  function write(value) {
    if (store) store.setItem(CREDENTIAL_KEY, value);
    else memory = value;
    announce();
  }

  return {
    status,
    headers,
    kind,
    /** What a human just typed. Trimmed, because a pasted credential carries whitespace. */
    remember(token) {
      write(String(token ?? "").trim());
    },
    /** Say out loud that this steward has no credential to present. */
    declareOpen() {
      write("");
    },
    forget() {
      if (store) store.removeItem(CREDENTIAL_KEY);
      else memory = null;
      announce();
    },
    /** True when the token lives only for this render — no storage would take it. */
    ephemeral: store === null,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
