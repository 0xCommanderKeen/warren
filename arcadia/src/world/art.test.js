import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { createArtKit } from "./art.js";

const meshes = group => {
  const result = [];
  group.traverse(object => { if (object.isMesh) result.push(object); });
  return result;
};

describe("miniature architecture", () => {
  it("keeps every architectural detail within the declared plot and above its base", () => {
    const art = createArtKit();
    try {
      // The narrow cases catch porch/roof/planter overhangs that can block streets.
      for (const [width, depth] of [[7, 5.5], [6, 4.5], [4, 4], [1.4, 1.4], [0.8, 1.2], [5, 1.1]]) {
        for (const kind of ["home", "lodge", "workshop", "archive", "noticeboard", "square"]) {
          for (const id of ["home:a", "home:b", "home:c", "home:d"]) {
            const group = art.building({ id, kind, width, depth });
            const bounds = new THREE.Box3().setFromObject(group);
            expect(bounds.min.y).toBeGreaterThanOrEqual(-0.00001);
            expect(bounds.max.y).toBeLessThan(4.5);
            expect(bounds.min.x).toBeGreaterThanOrEqual(-width / 2 - 0.00001);
            expect(bounds.max.x).toBeLessThanOrEqual(width / 2 + 0.00001);
            expect(bounds.min.z).toBeGreaterThanOrEqual(-depth / 2 - 0.00001);
            expect(bounds.max.z).toBeLessThanOrEqual(depth / 2 + 0.00001);
            expect(meshes(group).length).toBeLessThanOrEqual(40);
          }
        }
      }
    } finally { art.dispose(); }
  });

  it("retains personal home appearance when a resident changes projects", () => {
    const art = createArtKit();
    const appearance = project => meshes(art.building({ id: "home:pip", kind: "home", width: 4, depth: 4, project }))
      .map(mesh => [mesh.geometry.uuid, mesh.material.color.getHexString(), mesh.position.toArray(), mesh.scale.toArray()]);
    try { expect(appearance("chronicle")).toEqual(appearance("steward")); }
    finally { art.dispose(); }
  });

  it("shares geometry across scaled buildings and keeps clerestory windows controllable", () => {
    const art = createArtKit();
    try {
      const first = meshes(art.building({ id: "workshop", kind: "workshop", width: 7, depth: 5.5 }));
      const second = meshes(art.building({ id: "workshop", kind: "workshop", width: 5, depth: 4 }));
      expect(first.map(mesh => mesh.geometry)).toEqual(second.map(mesh => mesh.geometry));
      const glazing = first.filter(mesh => mesh.userData.architecture === "clerestory");
      expect(glazing.length).toBeGreaterThan(0);
      expect(glazing.every(mesh => mesh.userData.window)).toBe(true);
      const resources = new Set([...first, ...second].flatMap(mesh => [mesh.geometry, mesh.material]));
      const disposed = new Map();
      resources.forEach(resource => resource.addEventListener("dispose", () => disposed.set(resource, (disposed.get(resource) || 0) + 1)));
      art.dispose();
      expect([...resources].every(resource => disposed.get(resource) === 1)).toBe(true);
    } finally { art.dispose(); }
  });
});
