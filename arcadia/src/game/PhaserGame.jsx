import { useEffect, useLayoutEffect, useRef } from "react";

import { EventBus } from "./EventBus.js";
import { startGame } from "./startGame.js";

export function PhaserGame({ snapshot, onSceneReady }) {
  const host = useRef(null);
  const game = useRef(null);
  const initialSnapshot = useRef(snapshot);
  const appliedSnapshot = useRef(snapshot);

  useLayoutEffect(() => {
    game.current = startGame(host.current, initialSnapshot.current);
    return () => game.current.destroy(true);
  }, []);

  useEffect(() => {
    if (snapshot === appliedSnapshot.current) return;
    appliedSnapshot.current = snapshot;
    game.current.applySnapshot(snapshot);
  }, [snapshot]);

  useEffect(() => {
    if (!onSceneReady) return undefined;

    EventBus.on("current-scene-ready", onSceneReady);
    return () => EventBus.off("current-scene-ready", onSceneReady);
  }, [onSceneReady]);

  return <div className="absolute inset-0 [&_canvas]:block [&_canvas]:[image-rendering:pixelated]" ref={host} aria-hidden="true" />;
}
