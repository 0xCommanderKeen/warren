# Arcadia assets

Everything Vite copies verbatim into the build and Phaser loads at runtime from `/assets/`.

## Pixel art — CC0, credit the author anyway

Pixel art from the **Ninja Adventure Asset Pack** by [pixel-boy & AAA](https://pixel-boy.itch.io/ninja-adventure-asset-pack), licensed **CC0** (public domain). Thank you, pixel-boy — consider supporting the pack on itch.io.

- `tilesets/` — ground, nature, and house tilesets (16×16)
- `characters/<Name>-walk.png` — 64×64 sheets: 4 columns (facing down, up, left, right) × 4 walk frames
- `characters/<Name>-idle.png` — 64×16: one standing frame per direction
- `shadow.png` — blob shadow drawn under villagers

These arrived here from Burrow's retired in-tree viewer, which is where they were first
used. CC0 asks for nothing, but the attribution travels with the files regardless.

## Font

- `fonts/CousineSnapshot.ttf` — [Cousine](https://github.com/googlefonts/cousine), licensed
  under the SIL Open Font License 1.1. `fonts/CousineSnapshot-LICENSE.txt` is that license
  and must stay beside the font.

## Map and placeholder tiles

- `village.tmj` — the village map, authored as a Tiled JSON export. Arcadia's own.
- `village-tiles.svg` — placeholder tiles the scene renders today, kept until the pixel art
  above is wired into the scene. Arcadia's own.
