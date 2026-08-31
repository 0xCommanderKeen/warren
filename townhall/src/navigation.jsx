/* Navigation, prefix-aware.
 *
 * Every link in townhall goes through `<Link to>`, which takes an app route ("/skills")
 * and writes the address-bar path for whatever prefix this build was made under. Nothing
 * else in the app may touch `window.location` — that is how the `/observatory/` mount
 * stopped working the last time, and how it would stop working again.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { matchRoute, stripBase, withBase } from "./routes.js";

const NavigationContext = createContext(null);

export function useNavigation() {
  const value = useContext(NavigationContext);
  if (!value) throw new Error("useNavigation outside a NavigationProvider");
  return value;
}

export function NavigationProvider({ base, children }) {
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  const href = useCallback((route) => withBase(route, base), [base]);

  const navigate = useCallback(
    (route) => {
      const path = withBase(route, base);
      window.history.pushState({}, "", path);
      setPathname(path);
      window.scrollTo?.(0, 0);
    },
    [base],
  );

  const value = useMemo(() => {
    // A path outside the mount cannot be one of our routes; treat it as the root rather
    // than matching a page out of somebody else's URL.
    const route = stripBase(pathname, base) ?? "/";
    return { base, route, href, navigate, ...matchRoute(route) };
  }, [pathname, base, href, navigate]);

  return <NavigationContext.Provider value={value}>{children}</NavigationContext.Provider>;
}

export function Link({ to, children, className, ...props }) {
  const { href, navigate } = useNavigation();
  return (
    <a
      {...props}
      href={href(to)}
      className={className}
      onClick={(event) => {
        // Let a modified click do what the browser would do: open a tab, save the target.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
