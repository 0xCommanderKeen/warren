import Phaser from "phaser";

import mapJson from "../../public/assets/village.tmj?raw";
import { buildVillageModel } from "./villageModel.js";
import { EventBus } from "./EventBus.js";

const mapData = JSON.parse(mapJson);

function tintColor(value) {
  return Number.parseInt((value || "#d98961").slice(1), 16);
}

export function createVillageScene(snapshot) {
  return class VillageScene extends Phaser.Scene {
    constructor() {
      super("Village");
    }

    preload() {
      this.load.spritesheet("village-tiles", "/assets/village-tiles.svg", {
        frameWidth: 32,
        frameHeight: 32,
      });
    }

    create() {
      this.cache.tilemap.add("village", {
        data: mapData,
        format: Phaser.Tilemaps.Formats.TILED_JSON,
      });
      const map = this.make.tilemap({ key: "village" });
      const tiles = map.addTilesetImage("village-tiles", "village-tiles");
      map.createLayer("Terrain", tiles);
      this.createDepthSortedLayer(mapData, "Buildings");
      this.createDepthSortedLayer(mapData, "Collision");

      this.cameras.main.setBackgroundColor("#9db57a");
      this.cameras.main.setBounds(0, 0, map.widthInPixels, map.heightInPixels);
      this.cameras.main.centerOn(map.widthInPixels / 2, map.heightInPixels / 2);

      for (const villager of buildVillageModel(mapData, snapshot.villagers, snapshot.approvals)) {
        this.createHome(villager);
        this.createVillager(villager);
      }

      EventBus.emit("current-scene-ready", this);
    }

    createDepthSortedLayer(data, name) {
      const source = data.layers.find((item) => item.name === name);
      source.data.forEach((gid, index) => {
        if (!gid) return;
        const x = (index % data.width) * data.tilewidth + data.tilewidth / 2;
        const y = Math.floor(index / data.width) * data.tileheight + data.tileheight / 2;
        this.add.image(x, y, "village-tiles", gid - 1).setDepth(y + data.tileheight / 2);
      });
    }

    createHome(villager) {
      const isLodge = villager.dwelling.kind === "lodge";
      if (isLodge && this.lodgeCreated) return;
      if (isLodge) {
        this.lodgeCreated = true;
        this.add.text(villager.x, villager.y - 58, "LODGE", {
          color: "#253a2b", fontFamily: "monospace", fontSize: "8px",
        }).setOrigin(0.5).setDepth(villager.y - 1);
        return;
      }

      const x = villager.dwelling.x;
      const y = villager.dwelling.y;
      const width = 56;
      const height = 46;
      const house = this.add.container(x, y);
      house.add([
        this.add.rectangle(0, 0, width, height, 0xe5d5aa),
        this.add.triangle(0, -height / 2, -width / 2 - 5, 0, width / 2 + 5, 0, 0, -25, 0x704c3a),
        this.add.rectangle(0, height / 2 - 12, 14, 24, 0x634536),
        this.add.text(0, -2, villager.name.toUpperCase(), {
          color: "#253a2b", fontFamily: "monospace", fontSize: "8px",
        }).setOrigin(0.5),
      ]);
      house.setDepth(y + height / 2);
    }

    createVillager(villager) {
      const body = this.add.container(villager.x, villager.y);
      body.add([
        this.add.ellipse(0, -5, 13, 17, tintColor(villager.accent)),
        this.add.circle(0, -17, 6, 0xf0cda8),
        this.add.rectangle(0, 5, 15, 4, 0x24382a),
        this.add.text(0, 10, villager.name, {
          color: "#f8f1dc",
          backgroundColor: "#26392dcc",
          fontFamily: "monospace",
          fontSize: "9px",
          padding: { x: 3, y: 2 },
        }).setOrigin(0.5, 0),
      ]);
      body.setDepth(villager.y);

      if (!villager.moving) return;
      const targets = villager.route.map((point) => ({
        targets: body,
        x: point.x,
        y: point.y,
        duration: 1800,
        onUpdate: () => body.setDepth(body.y),
      }));
      const returnHome = {
        targets: body,
        x: villager.x,
        y: villager.y,
        duration: 1800,
        onUpdate: () => body.setDepth(body.y),
      };
      this.tweens.chain({ tweens: [...targets, ...targets.toReversed(), returnHome], repeat: -1 });
    }
  };
}
