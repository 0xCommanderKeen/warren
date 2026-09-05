import Phaser from "phaser";
import { createVillageScene } from "./VillageScene.js";

export function startGame(parent, snapshot) {
  const Scene = createVillageScene(snapshot);
  const scene = new Scene();
  const game = new Phaser.Game({
    type: Phaser.AUTO,
    parent,
    width: 640,
    height: 384,
    backgroundColor: "#d8c59e",
    scale: {
      mode: Phaser.Scale.FIT,
      autoCenter: Phaser.Scale.CENTER_BOTH,
    },
    pixelArt: true,
    scene: [scene],
  });
  return {
    selectVillager(id) { scene.selectVillager(id); },
    setSelectionHandler(callback) { scene.onSelectVillager = callback; },
    setMotionPaused(paused) { scene.setMotionPaused(paused); },
    applySnapshot(next) {
      scene.applySnapshot(next);
    },
    destroy(removeCanvas) {
      game.destroy(removeCanvas);
    },
  };
}
