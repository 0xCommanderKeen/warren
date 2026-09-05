import { useEffect, useRef } from "react";
import { createInteriorRenderer } from "./interior.js";

/** Accessible occupant navigation is supplied by the room's surrounding panel. */
export function InteriorWorld({ building, agents, paused = false, quality = "high", cameraCommand, onSelect, onError }) {
  const host = useRef(null);
  const renderer = useRef(null);
  const callbacks = useRef({ onSelect, onError });
  callbacks.current = { onSelect, onError };
  const state = useRef(null);
  state.current = { building, agents, paused, quality, cameraCommand };
  useEffect(() => {
    let instance;
    try {
      instance = createInteriorRenderer(host.current, {
        onSelect: selection => callbacks.current.onSelect?.(selection),
        onError: error => callbacks.current.onError?.(error),
      });
      instance.update(state.current);
      renderer.current = instance;
    } catch (error) {
      instance?.dispose();
      callbacks.current.onError?.(error);
    }
    return () => { instance?.dispose(); renderer.current = null; };
  }, []);
  useEffect(() => {
    try { renderer.current?.update(state.current); }
    catch (error) { callbacks.current.onError?.(error); }
  }, [building, agents, paused, quality, cameraCommand]);
  return <div className="interior-world" ref={host} aria-hidden="true" style={{ width: "100%", height: "100%", minHeight: 360, position: "relative", overflow: "hidden" }} />;
}
