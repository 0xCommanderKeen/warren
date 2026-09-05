# Living village

Arcadia presents Chronicle's real agents as a miniature village. Original geometric art keeps buildings, colors, and population expandable without sprite-sheet or tile-map constraints.

## Boundaries

- `layout.js` allocates stable homes, one shared workshop, visitor lodges, civic buildings, and connected roads. Retired allocations and explored bounds remain stable during transitions. Its optional versioned serialization validates coordinates, slot uniqueness, bounds, and size before restoring a layout.
- `motion.js` advances finite paths. Retargeting preserves the remaining road route before joining the new one; pause snaps to the current destination. It does not invent work or perpetual wandering.
- `art.js` supplies reusable geometry and materials. Building factories fit their declared footprint, so new styles need no layout changes.
- `InteriorWorld.jsx` presents furnished cutaway rooms and the current occupants of a home, lodge, or shared workshop. Room occupants come from Chronicle destinations; empty rooms remain explorable. The exterior scene stays mounted while inside, preserving navigation.
- `renderer.js` owns the WebGL scene, camera, picking, projected labels, instancing, and resource cleanup. `VillageWorld.jsx` is its React lifecycle boundary.
- `VillageExperience.jsx` owns accessible selection, search, dossiers, attention, camera commands, and optional local persistence under `arcadia:village-layout:v1`. Storage failures are nonfatal. Chronicle and Steward remain the read and write authority boundaries.

Home positions persist per browser and origin, not across devices. Stored data includes identifiers and allocations, not names, messages, credentials, or history. Clearing storage resets placement. Population removal does not reclaim plots immediately, preserving spatial continuity.

## Presentation and truth

Local-clock light blends between dawn, day, dusk, and night. Actual working/resting occupancy controls evening windows. State determines destinations; initial load places agents at their current destination without invented historical travel. Indoor occupants are hidden outside once at their destination; clicking buildings or selecting an indoor agent opens the appropriate room. Notices require new snapshot generations and do not replay on initial load or log reset. The scene supplies no fabricated conversations, tasks, weather, or social events.

The directory and civic buttons expose scene selections to keyboard and assistive-technology users. Reduced motion starts paused; lighter rendering disables shadows and limits frame frequency. WebGL failure keeps the operational interface available.

## Verification and limits

Unit tests cover layout growth, persistence validation, state destinations, interrupted routes, daylight, UI selection, storage failure, Chronicle contracts, and Steward authority. Production browser tests exercise 320/390/768/1440-pixel viewports, stream changes, 0/5/25/100 agents, reduced motion, and actual WebGL context loss. Room regressions cover 0/5/100 workshop occupants, home/lodge entry, live occupancy changes, preserving the exterior canvas on return, and usable room rosters after graphics loss.

On 2026-09-05, a local production benchmark in desktop Chrome at 1440×1000 held 60 fps for 5, 25, and 100 agents (five projects). Dynamic instancing reduced the 100-agent scene from 2,062 draw calls to approximately 100. These measurements establish behavior on the development machine; physical low-end phones and larger populations have not been benchmarked. Browser mobile viewport checks verify layout, not mobile GPU performance.

Static scenery is batched by geometry/material; agent meshes share dynamic instance batches with individual colors and picking IDs. Snapshot changes retain motion and selections. Hidden tabs skip rendering, and disposal releases renderer and scene resources.
