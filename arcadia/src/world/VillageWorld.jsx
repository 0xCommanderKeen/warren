import { useEffect, useRef } from "react";
import { createVillageRenderer } from "./renderer.js";
import "./world.css";

export function VillageWorld({
  world,
  selection,
  onSelect,
  paused,
  quality,
  follow,
  cameraCommand,
  onReady,
  onError,
  onCameraChange,
}) {
  const host = useRef(null);
  const renderer = useRef(null);
  const callbacks = useRef({ onSelect, onReady, onError, onCameraChange });
  callbacks.current = { onSelect, onReady, onError, onCameraChange };
  const state = useRef(null);
  state.current = { world, selection, paused, quality, follow, cameraCommand };
  useEffect(() => {
    let instance;
    try {
      instance = createVillageRenderer(host.current, {
        onSelect: (value) => callbacks.current.onSelect?.(value),
        onError: (error) => callbacks.current.onError?.(error),
        onCameraChange: (pose) => callbacks.current.onCameraChange?.(pose),
      });
      renderer.current = instance;
      instance.update(state.current);
      callbacks.current.onReady?.();
    } catch (error) {
      instance?.dispose();
      callbacks.current.onError?.(error);
    }
    return () => {
      instance?.dispose();
      renderer.current = null;
    };
  }, []);
  useEffect(() => {
    try {
      renderer.current?.update(state.current);
    } catch (error) {
      callbacks.current.onError?.(error);
    }
  }, [world, selection, paused, quality, follow, cameraCommand]);
  return (
    <div
      className="village-world"
      ref={host}
      data-testid="village-canvas"
      aria-hidden="true"
    />
  );
}
