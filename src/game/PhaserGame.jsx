import { useLayoutEffect, useRef } from "react";

import { startGame } from "./startGame.js";

export function PhaserGame() {
  const host = useRef(null);

  useLayoutEffect(() => {
    const game = startGame(host.current);
    return () => game.destroy(true);
  }, []);

  return <div className="village__canvas" ref={host} aria-hidden="true" />;
}
