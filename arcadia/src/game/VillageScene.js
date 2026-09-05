import Phaser from "phaser";
import mapJson from "../../public/assets/village.tmj?raw";
import { buildVillageModel } from "./villageModel.js";
import { characters, characterName } from "./characters.js";
import { EventBus } from "./EventBus.js";

const mapData = JSON.parse(mapJson);
const stateColors = {
  working: 0x78ad62,
  knocking: 0xe5aa51,
  failed: 0xd87959,
  stale: 0xa6aa98,
  resting: 0x8bb4c4,
  idle: 0x8bb4c4,
};
const textStyle = {
  color: "#f9f6df",
  fontFamily: "Cousine, monospace",
  fontSize: "8px",
  backgroundColor: "#304a39",
  padding: { x: 4, y: 3 },
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function createVillageScene(snapshot) {
  return class VillageScene extends Phaser.Scene {
    constructor() {
      super("Village");
      this.snapshot = snapshot;
      this.villagers = new Map();
      this.selectedId = null;
      this.motionPaused = false;
    }

    preload() {
      this.load.spritesheet("village-tiles", "/assets/village-tiles.svg", {
        frameWidth: 32,
        frameHeight: 32,
      });
      this.load.image("houses", "/assets/tilesets/TilesetHouse.png");
      this.load.image("nature", "/assets/tilesets/TilesetNature.png");
      this.load.image("shadow", "/assets/shadow.png");
      for (const name of characters) {
        this.load.spritesheet(
          `${name}-idle`,
          `/assets/characters/${name}-idle.png`,
          { frameWidth: 16, frameHeight: 16 },
        );
        this.load.spritesheet(
          `${name}-walk`,
          `/assets/characters/${name}-walk.png`,
          { frameWidth: 16, frameHeight: 16 },
        );
      }
    }

    create() {
      this.textures.get("houses").add("cottage", 0, 0, 0, 64, 64);
      this.textures.get("houses").add("lodge", 0, 192, 0, 64, 48);
      this.textures.get("nature").add("tree", 0, 0, 0, 32, 32);
      this.textures.get("nature").add("pine", 0, 0, 32, 64, 48);
      this.textures.get("nature").add("flowers", 0, 0, 160, 16, 16);
      this.cache.tilemap.add("village", {
        data: mapData,
        format: Phaser.Tilemaps.Formats.TILED_JSON,
      });
      const map = this.make.tilemap({ key: "village" });
      const tiles = map.addTilesetImage("village-tiles", "village-tiles");
      map.createLayer("Terrain", tiles);
      this.createLandscape();
      this.cameras.main.setBackgroundColor("#9aaa69");
      for (const name of characters) {
        for (let direction = 0; direction < 4; direction++) {
          this.anims.create({
            key: `${name}-${direction}`,
            frames: [0, 4, 8, 12].map((frame) => ({
              key: `${name}-walk`,
              frame: frame + direction,
            })),
            frameRate: 7,
            repeat: -1,
          });
        }
      }
      this.created = true;
      this.renderSnapshot();
      this.setMotionPaused(this.motionPaused);
      EventBus.emit("current-scene-ready", this);
    }

    createLandscape() {
      const collision = mapData.layers.find((l) => l.name === "Collision");
      collision.data.forEach((gid, index) => {
        if (!gid) return;
        const x = (index % mapData.width) * 32 + 16;
        const y = Math.floor(index / mapData.width) * 32 + 24;
        this.add
          .image(x, y, "nature", "tree")
          .setOrigin(0.5, 1)
          .setScale(1.35)
          .setDepth(y);
      });
      for (const point of mapData.layers.find((l) => l.name === "Scenery")
        ?.objects ?? []) {
        this.add
          .image(point.x, point.y, "nature", point.type)
          .setOrigin(0.5, 1)
          .setDepth(point.y);
      }
      const lodge = mapData.layers
        .find((l) => l.name === "Places")
        .objects.find((p) => p.name === "Lodge");
      this.add
        .image(lodge.x, lodge.y + 4, "houses", "lodge")
        .setOrigin(0.5, 1)
        .setScale(1.15)
        .setDepth(lodge.y);
      this.add
        .text(lodge.x, lodge.y - 59, "THE LODGE", {
          ...textStyle,
          color: "#f4eac3",
          fontSize: "7px",
        })
        .setOrigin(0.5)
        .setDepth(lodge.y + 1);
      this.add
        .text(320, 171, "VILLAGE GREEN", {
          color: "#6d7850",
          fontFamily: "Cousine, monospace",
          fontSize: "7px",
          letterSpacing: 2,
        })
        .setOrigin(0.5)
        .setDepth(0);
    }

    applySnapshot(next) {
      this.snapshot = next;
      if (this.created) this.renderSnapshot();
    }

    renderSnapshot() {
      const models = buildVillageModel(
        mapData,
        this.snapshot.villagers,
        this.snapshot.approvals,
      );
      const ids = new Set(models.map((v) => v.id));
      for (const [id, entry] of this.villagers) {
        if (ids.has(id)) continue;
        entry.tween?.destroy();
        entry.body.destroy();
        entry.home?.destroy();
        this.villagers.delete(id);
      }
      for (const model of models) {
        const entry = this.villagers.get(model.id);
        if (!entry) {
          const created = this.createVillager(model);
          created.home = this.createHome(model);
          this.villagers.set(model.id, created);
          this.moveVillager(created, model);
        } else {
          const before = entry.model;
          if (!same(before.dwelling, model.dwelling)) {
            entry.home?.destroy();
            entry.home = this.createHome(model);
          }
          entry.label.setText(model.name);
          entry.home?.getByName("home-label")?.setText(model.name);
          entry.dot.setFillStyle(stateColors[model.state] ?? stateColors.idle);
          entry.body.setAlpha(model.state === "stale" ? 0.55 : 1);
          entry.model = model;
          if (before.char !== model.char) {
            entry.sprite
              .stop()
              .setTexture(`${characterName(model.char)}-idle`, 0);
          }
          if (
            before.moving !== model.moving ||
            before.x !== model.x ||
            before.y !== model.y ||
            !same(before.route, model.route) ||
            before.char !== model.char
          ) {
            this.moveVillager(entry, model);
          }
        }
      }
      this.selectVillager(this.selectedId);
    }

    createHome(model) {
      if (model.dwelling.kind === "lodge") return null;
      const { x, y } = model.dwelling;
      const home = this.add.container(x, y).setDepth(y);
      home.add([
        this.add.image(0, 4, "houses", "cottage").setOrigin(0.5, 1),
        this.add
          .text(0, -67, model.name, {
            ...textStyle,
            backgroundColor: "#526345",
            fontSize: "7px",
          })
          .setOrigin(0.5)
          .setName("home-label"),
      ]);
      return home;
    }

    createVillager(model) {
      const body = this.add
        .container(model.x, model.y)
        .setDepth(model.y)
        .setAlpha(model.state === "stale" ? 0.55 : 1);
      const ring = this.add
        .ellipse(0, 1, 25, 11, 0xf6e9aa, 0)
        .setStrokeStyle(1, 0xffefb5)
        .setVisible(false);
      const sprite = this.add
        .sprite(0, -11, `${characterName(model.char)}-idle`, 0)
        .setScale(1.5);
      const dot = this.add.circle(
        10,
        -23,
        2.5,
        stateColors[model.state] ?? stateColors.idle,
      );
      const label = this.add
        .text(0, -37, model.name, textStyle)
        .setOrigin(0.5)
        .setVisible(false);
      body.add([
        this.add.image(0, 0, "shadow").setAlpha(0.3),
        ring,
        sprite,
        dot,
        label,
      ]);
      body.setSize(26, 34).setInteractive({ useHandCursor: true });
      body.on("pointerover", () => {
        label.setVisible(true);
        body.setDepth(1000);
      });
      body.on("pointerout", () => {
        label.setVisible(this.selectedId === model.id);
        body.setDepth(body.y);
      });
      body.on("pointerdown", () => this.onSelectVillager?.(model.id));
      return { body, sprite, dot, label, ring, model, tween: null, home: null };
    }

    moveVillager(entry, model) {
      entry.tween?.destroy();
      entry.tween = null;
      entry.sprite.stop().setTexture(`${characterName(model.char)}-idle`, 0);
      const destination = { x: model.x, y: model.y };
      if (!model.moving && entry.body.x === model.x && entry.body.y === model.y)
        return;
      const points = model.moving
        ? [
            ...model.route,
            ...model.route.slice(0, -1).toReversed(),
            destination,
          ]
        : [destination];
      const name = characterName(model.char);
      let previous = entry.body;
      const steps = points.map((point) => {
        const duration = Math.max(
          120,
          (Math.hypot(point.x - previous.x, point.y - previous.y) / 28) * 1000,
        );
        previous = point;
        return {
          targets: entry.body,
          x: point.x,
          y: point.y,
          duration,
          onStart: () => {
            const dx = point.x - entry.body.x,
              dy = point.y - entry.body.y;
            const direction =
              Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? 2 : 3) : dy < 0 ? 1 : 0;
            entry.sprite.play(`${name}-${direction}`, true);
          },
          onUpdate: () => entry.body.setDepth(entry.body.y),
        };
      });
      entry.tween = this.tweens.chain({
        tweens: steps,
        repeat: model.moving ? -1 : 0,
        onComplete: () => {
          entry.tween = null;
          entry.sprite.stop().setTexture(`${name}-idle`, 0);
        },
      });
      if (this.motionPaused) {
        entry.tween.pause();
        entry.sprite.anims.pause();
      }
    }

    selectVillager(id) {
      this.selectedId = id;
      for (const [villagerId, entry] of this.villagers) {
        entry.ring.setVisible(villagerId === id);
        entry.label.setVisible(villagerId === id);
      }
    }

    setMotionPaused(paused) {
      this.motionPaused = paused;
      if (!this.created) return;
      for (const entry of this.villagers.values()) {
        if (paused) {
          entry.tween?.pause();
          entry.sprite.anims.pause();
        } else {
          entry.tween?.resume();
          entry.sprite.anims.resume();
        }
      }
    }
  };
}
