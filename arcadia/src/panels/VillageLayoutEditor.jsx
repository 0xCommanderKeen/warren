import { useState } from "react";
import "./village-layout-editor.css";

export function VillageLayoutEditor({ world, onMoveBuilding, onUndoMove, onReset }) {
  const [selectedId, setSelectedId] = useState(world.buildings[0]?.id || "");
  const [message, setMessage] = useState("");
  const selected = world.buildings.find(building => building.id === selectedId) || world.buildings[0];
  if (!selected) return null;
  const move = position => {
    const result = onMoveBuilding?.(selected.id, position);
    setMessage(result?.ok ? result.changed ? `${selected.name} moved. Saved in this browser.` : "This building is already on that plot." : result?.error || "The building could not be moved.");
  };
  const limit = world.editLimit || 40;
  const plots = [];
  for (let z = selected.position[1] - 30; z <= selected.position[1] + 30; z += 10) {
    for (let x = selected.position[0] - 30; x <= selected.position[0] + 30; x += 10) {
      const occupant = world.buildings.find(building => building.position[0] === x && building.position[1] === z);
      plots.push({ x, z, occupant, outside: Math.abs(x) > limit || Math.abs(z) > limit });
    }
  }
  return <section className="village-layout-editor" aria-label="Edit village layout">
    <header><h3>Arrange your village</h3><p>Choose a building, then an empty plot. Streets reconnect automatically. Changes stay in this browser.</p></header>
    <label>Building<select value={selected.id} onChange={event => { setSelectedId(event.target.value); setMessage(""); }}>{world.buildings.map(building => <option key={building.id} value={building.id}>{building.name}</option>)}</select></label>
    <div className="layout-plot-grid" role="group" aria-label="Nearby village plots">
      {plots.map(({ x, z, occupant, outside }) => <button key={`${x}:${z}`} type="button" disabled={outside}
        className={occupant?.id === selected.id ? "selected" : occupant ? "occupied" : "free"}
        aria-label={outside ? `Plot ${x}, ${z} outside editing area` : occupant ? `${occupant.name} at ${x}, ${z}` : `Move ${selected.name} to ${x}, ${z}`}
        aria-pressed={occupant?.id === selected.id}
        title={occupant?.name || `Plot ${x}, ${z}`}
        onClick={() => occupant ? setSelectedId(occupant.id) : move([x, z])}>{occupant ? occupant.kind === "home" ? "⌂" : occupant.kind === "workshop" ? "⚒" : "◆" : outside ? "" : "+"}</button>)}
    </div>
    <div className="layout-direction-buttons" role="group" aria-label="Move selected building">
      {[[0, -10, "North", "↑"], [-10, 0, "West", "←"], [10, 0, "East", "→"], [0, 10, "South", "↓"]].map(([x, z, name, symbol]) => <button key={name} type="button" aria-label={`Move ${name.toLowerCase()}`} onClick={() => move([selected.position[0] + x, selected.position[1] + z])}>{symbol} {name}</button>)}
    </div>
    <div className="layout-editor-actions"><button type="button" disabled={!world.canUndoLayoutMove} onClick={() => { const result = onUndoMove?.(); setMessage(result?.ok ? "Last move undone." : result?.error || "Could not undo the move."); }}>Undo last move</button><button type="button" onClick={() => { const result = onReset?.(); setMessage(result?.ok === false ? result.error : "Default layout restored. Saved in this browser."); }}>Reset layout</button></div>
    <p className="layout-editor-status" role="status">{message || `${selected.name} · plot ${selected.position.join(", ")}`}</p>
  </section>;
}
