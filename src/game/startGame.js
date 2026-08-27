import Phaser from "phaser";

import { EventBus } from "./EventBus.js";

class VillageShell extends Phaser.Scene {
  constructor() {
    super("VillageShell");
  }

  create() {
    EventBus.emit("current-scene-ready", this);
  }
}

export function startGame(parent) {
  return new Phaser.Game({
    type: Phaser.AUTO,
    parent,
    width: 1280,
    height: 720,
    backgroundColor: "#d8c59e",
    scene: VillageShell,
    scale: {
      mode: Phaser.Scale.RESIZE,
      autoCenter: Phaser.Scale.CENTER_BOTH,
    },
  });
}
