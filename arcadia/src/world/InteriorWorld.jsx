import { useEffect, useMemo, useRef } from "react";
import { createInteriorRenderer } from "./interior.js";
import { createRoomLayout } from "./roomLayout.js";

const STORAGE_KEY = "arcadia:room-layout:v1";
function restoreLayout() {
  try {
    const text = localStorage.getItem(STORAGE_KEY);
    return createRoomLayout(text && text.length <= 1000000 ? JSON.parse(text) : null);
  } catch { return createRoomLayout(); }
}

/** Accessible occupant navigation is supplied by the room's surrounding panel. */
export function InteriorWorld({ building, agents, villagers = agents, paused = false, quality = "high", cameraCommand, focusAgentId = null, onSelect, onError }) {
  const host = useRef(null);
  const renderer = useRef(null);
  const layout = useRef(null);
  if (!layout.current) layout.current = restoreLayout();
  const room = useMemo(() => layout.current.update(building.id, agents, villagers), [building.id, agents, villagers]);
  useEffect(() => {
    try {
      const saved = layout.current.serialize();
      if (saved) localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
    } catch { /* Private browsing and storage limits must not prevent room visits. */ }
  }, [room]);
  const callbacks = useRef({ onSelect, onError });
  callbacks.current = { onSelect, onError };
  const state = useRef(null);
  state.current = { building, agents, room, paused, quality, cameraCommand, focusAgentId };
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
  }, [building, agents, room, paused, quality, cameraCommand, focusAgentId]);
  return <div className="interior-world" ref={host} aria-hidden="true" style={{ width: "100%", height: "100%", minHeight: 360, position: "relative", overflow: "hidden" }} />;
}
