import { createPortal } from "react-dom";
import { useEffect } from "react";
import "./village-navigator.css";

export function VillageNavigator({
  active = true,
  world,
  selection,
  onSelect,
  onOverview,
  camera,
  roomId,
  visible,
  mapHost,
}) {
  const destinations = [
    { kind: "workshop", label: "Workshop", digit: "1" },
    { kind: "lodge", label: "Lodge", digit: "2" },
    { kind: "archive", label: "Archive", digit: "3" },
  ].map((item) => ({
    ...item,
    building: world.buildings.find((building) => building.kind === item.kind),
  }));
  useEffect(() => {
    if (!active) return;
    function keydown(event) {
      const target = event.target;
      if (
        !event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.repeat ||
        event.isComposing ||
        event.defaultPrevented ||
        target?.closest?.(
          "input,textarea,select,[contenteditable]:not([contenteditable='false'])",
        )
      )
        return;
      const digit = /^Digit[0-3]$/.test(event.code)
        ? event.code.slice(-1)
        : event.key;
      if (digit === "0") {
        event.preventDefault();
        onOverview();
        return;
      }
      const destination = destinations.find(
        (item) => item.digit === digit,
      )?.building;
      if (destination) {
        event.preventDefault();
        onSelect({ kind: "building", id: destination.id });
      }
    }
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [active, world, onSelect, onOverview]);
  const showMap = visible ?? (Boolean(roomId) || camera?.zoom > 1.4);
  const { minX, maxX, minZ, maxZ } = world.bounds;
  const selectedAgent =
    selection?.kind === "agent"
      ? world.agents.find((agent) => agent.id === selection.id)
      : null;
  const activeId =
    roomId ||
    (selection?.kind === "building" ? selection.id : selectedAgent?.buildingId);
  function activate(event, building) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect({ kind: "building", id: building.id });
    }
  }
  const mountMap = node => mapHost ? createPortal(node, mapHost) : node;
  return (
    <nav className="village-navigator" aria-label="Village navigation">
      <div className="navigator-shortcuts">
        <button
          title="Village overview · Alt+0"
          aria-label="Show village overview"
          aria-keyshortcuts="Alt+0"
          onClick={onOverview}
        >
          ⌂ <span>Overview</span>
        </button>
        {destinations
          .filter((item) => item.building)
          .map((item) => (
            <button
              key={item.kind}
              aria-label={`Go to ${item.label}`}
              aria-keyshortcuts={`Alt+${item.digit}`}
              title={`${item.label} · Alt+${item.digit}`}
              onClick={() =>
                onSelect({ kind: "building", id: item.building.id })
              }
            >
              {item.label}
            </button>
          ))}
        <small className="navigator-key-hint">Alt + 0–3</small>
      </div>
      {showMap && mountMap(
        <div className="navigator-map">
          <header>
            <span>The whole village</span>
            <small>
              {roomId ? "Inside" : `${Math.round((camera?.zoom || 1) * 100)}%`}
            </small>
          </header>
          <svg
            viewBox={`${minX - 3} ${minZ - 3} ${maxX - minX + 6} ${maxZ - minZ + 6}`}
            role="group"
            aria-label="Village overview map"
          >
            <rect
              x={minX - 3}
              y={minZ - 3}
              width={maxX - minX + 6}
              height={maxZ - minZ + 6}
              rx="4"
              fill="#e8eedb"
            />
            <g stroke="#d4d7bd" strokeLinecap="round" aria-hidden="true">
              {world.roads.map((road, index) => (
                <line
                  key={index}
                  x1={road.from[0]}
                  y1={road.from[1]}
                  x2={road.to[0]}
                  y2={road.to[1]}
                  strokeWidth={road.width}
                />
              ))}
            </g>
            {world.buildings.map((building) => (
              <g
                key={building.id}
                role="button"
                tabIndex={0}
                aria-label={`Locate ${building.name} on map`}
                aria-pressed={activeId === building.id}
                className="navigator-map-building"
                onClick={() => onSelect({ kind: "building", id: building.id })}
                onKeyDown={(event) => activate(event, building)}
              >
                <title>{building.name}</title>
                <rect
                  x={building.position[0] - building.width / 2}
                  y={building.position[1] - building.depth / 2}
                  width={building.width}
                  height={building.depth}
                  rx=".9"
                  fill={
                    activeId === building.id
                      ? "#3f674b"
                      : building.kind === "home"
                        ? "#b49572"
                        : "#b96c4c"
                  }
                  stroke={activeId === building.id ? "#f8f5df" : "#8c795f"}
                  strokeWidth={activeId === building.id ? 0.8 : 0.35}
                />
              </g>
            ))}
            {camera?.target && !roomId && (
              <g aria-hidden="true" pointerEvents="none">
                <circle
                  cx={camera.target[0]}
                  cy={camera.target[2]}
                  r="2.5"
                  fill="none"
                  stroke="#527559"
                  strokeWidth=".5"
                />
                <circle
                  cx={camera.target[0]}
                  cy={camera.target[2]}
                  r=".65"
                  fill="#527559"
                />
              </g>
            )}
          </svg>
          <p>Alt + 0 overview · 1 workshop · 2 lodge · 3 archive</p>
        </div>
      )}
    </nav>
  );
}
