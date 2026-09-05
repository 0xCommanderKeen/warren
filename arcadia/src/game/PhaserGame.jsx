import { useEffect, useLayoutEffect, useRef } from "react";

import { EventBus } from "./EventBus.js";
import { startGame } from "./startGame.js";

export function PhaserGame({ snapshot, onSceneReady, selectedId = null, onSelectVillager, paused = false }) {
  const host = useRef(null);
  const game = useRef(null);
  const selectCallback = useRef(onSelectVillager);
  selectCallback.current = onSelectVillager;
  const initialSnapshot = useRef(snapshot);
  const appliedSnapshot = useRef(snapshot);

  useLayoutEffect(() => {
    game.current = startGame(host.current, initialSnapshot.current);
    game.current.setSelectionHandler?.(id => selectCallback.current?.(id));
    return () => game.current.destroy(true);
  }, []);

  useEffect(() => {
    if (snapshot === appliedSnapshot.current) return;
    appliedSnapshot.current = snapshot;
    game.current.applySnapshot(snapshot);
  }, [snapshot]);

  useEffect(() => { game.current.selectVillager?.(selectedId); }, [selectedId]);
  useEffect(() => { game.current.setMotionPaused?.(paused); }, [paused]);

  useEffect(() => {
    if (!onSceneReady) return undefined;

    EventBus.on("current-scene-ready", onSceneReady);
    return () => EventBus.off("current-scene-ready", onSceneReady);
  }, [onSceneReady]);

  return <div className="absolute inset-0 [&_canvas]:block [&_canvas]:[image-rendering:pixelated]" ref={host} aria-hidden="true" />;
}
