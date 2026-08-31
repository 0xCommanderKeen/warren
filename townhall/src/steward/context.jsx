/* Steward, as React sees it.
 *
 * One client and one credential for the whole app. Pages ask for data with
 * `useStewardQuery`, which does nothing at all until a credential exists — the read paths
 * on Chronicle's `/state` are untouched by any of this, so the fleet page keeps working
 * for somebody who never unlocks the write path.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { createOperatorCredential } from "./credential.js";
import { createStewardClient } from "./client.js";

const StewardContext = createContext(null);

export function useSteward() {
  const value = useContext(StewardContext);
  if (!value) throw new Error("useSteward outside a StewardProvider");
  return value;
}

/**
 * Where steward lives. This origin, unless a developer running vite points somewhere else.
 *
 * The override is behind `import.meta.env.DEV`, which Vite resolves to `false` at build
 * time: the branch below is eliminated from the bundle, so a deployed townhall has no
 * `?steward=` to honour. It used to honour it from any link, and the operator token in
 * sessionStorage is attached to every steward call — so `/observatory/?steward=https://evil.tld`
 * opened in an unlocked tab handed the control plane's master key to whoever wrote the
 * link (warren#241). A dev convenience that ships is not a dev convenience.
 */
function stewardBase() {
  if (!import.meta.env.DEV) return "";
  try {
    return new URLSearchParams(window.location.search).get("steward") || "";
  } catch {
    return "";
  }
}

export function StewardProvider({ children, storage, fetch: fetchImpl }) {
  const credential = useMemo(
    () => createOperatorCredential({ storage: storage ?? globalThis.sessionStorage }),
    [storage],
  );
  const baseUrl = useMemo(stewardBase, []);
  const client = useMemo(
    () => createStewardClient({ baseUrl, credential, fetch: fetchImpl }),
    [baseUrl, credential, fetchImpl],
  );
  const [status, setStatus] = useState(() => credential.status());

  useEffect(() => credential.subscribe(setStatus), [credential]);

  const value = useMemo(
    () => ({ client, credential, status, baseUrl, locked: status === "unknown" }),
    [client, credential, status, baseUrl],
  );
  return <StewardContext.Provider value={value}>{children}</StewardContext.Provider>;
}

/**
 * One read from steward, re-run when its dependencies change or somebody asks.
 *
 * `load` is called with an AbortSignal. A refusal is kept as the error rather than
 * swallowed — every page renders steward's own words for it.
 */
export function useStewardQuery(load, deps = []) {
  const { locked } = useSteward();
  const [state, setState] = useState({ data: null, error: null, loading: !locked });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    if (locked) {
      setState({ data: null, error: null, loading: false });
      return undefined;
    }
    const controller = new AbortController();
    let live = true;
    setState((previous) => ({ ...previous, loading: true }));
    Promise.resolve()
      .then(() => loadRef.current(controller.signal))
      .then(
        (data) => live && setState({ data, error: null, loading: false }),
        (error) => {
          if (!live || controller.signal.aborted) return;
          setState({ data: null, error, loading: false });
        },
      );
    return () => {
      live = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [locked, nonce, ...deps]);

  const refresh = useCallback(() => setNonce((value) => value + 1), []);
  return { ...state, refresh };
}
