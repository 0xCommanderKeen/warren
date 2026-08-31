import { useEffect, useLayoutEffect, useRef } from "react";

import { EventBus } from "./EventBus.js";
import { startGame } from "./startGame.js";

export function PhaserGame({ snapshot, onSceneReady }) {
  const host = useRef(null);

  useLayoutEffect(() => {
    const game = startGame(host.current, snapshot);
    return () => game.destroy(true);
  }, [snapshot]);

  useEffect(() => {
    if (!onSceneReady) return undefined;

    EventBus.on("current-scene-ready", onSceneReady);
    return () => EventBus.off("current-scene-ready", onSceneReady);
  }, [onSceneReady]);

  return <div className="absolute inset-0 [&_canvas]:block [&_canvas]:[image-rendering:pixelated]" ref={host} aria-hidden="true" />;
}
