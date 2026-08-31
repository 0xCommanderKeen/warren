/* The operator credential, held client-side and nowhere else.
 *
 * Townhall's reads come from Chronicle's public `/state`; its writes go to steward, which
 * gates every route on a bearer token. That token is typed by the human at runtime and
 * kept in this tab's `sessionStorage` — never baked into the bundle, never a session
 * credential (steward answers 403 on every write to one of those, by design).
 *
 * This module is deliberately the only thing that knows *how* the credential is obtained
 * and carried. `createStewardClient` takes any object with `status()` and `headers()`, so
 * warren#225 — whose criterion is that the master token never lands in a browser again —
 * can swap in a real operator credential without touching a single call site.
 */

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
   * "held"    — a token is in hand.
   * "open"    — the human said out loud this steward runs open (`serve --allow-open`),
   *             stored as the empty string: known, and known to be nothing.
   */
  function status() {
    const value = read();
    if (value === null || value === undefined) return "unknown";
    return value === "" ? "open" : "held";
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
    /** The token a human just typed. Trimmed, because a pasted token carries whitespace. */
    remember(token) {
      write(String(token ?? "").trim());
    },
    /** Say out loud that this steward has no token to present. */
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
