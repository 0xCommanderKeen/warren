import { useState } from "react";
import { PhaserGame } from "../game/PhaserGame.jsx";
import { characterName } from "../game/characters.js";

function Portrait({ villager }) {
  return (
    <span
      className="portrait"
      style={{
        backgroundImage: `url('/assets/characters/${characterName(villager.char)}-idle.png')`,
        "--accent": villager.accent,
      }}
      aria-hidden="true"
    />
  );
}

export function VillageOverview({ snapshot }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [selectedId, setSelectedId] = useState(null);
  const [paused, setPaused] = useState(
    () =>
      globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ??
      false,
  );
  const selected = snapshot.villagers.find((v) => v.id === selectedId);
  const villagers = snapshot.villagers
    .filter(
      (v) =>
        (filter === "all" ||
          (filter === "residents"
            ? v.residency === "resident"
            : v.state === "working")) &&
        `${v.name} ${v.project} ${v.state}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    )
    .toSorted(
      (a, b) =>
        Number(b.residency === "resident") -
          Number(a.residency === "resident") ||
        a.name.localeCompare(b.name) ||
        a.id.localeCompare(b.id),
    );

  return (
    <div id="village" className="village-layout">
      <section className="village-map-panel" aria-label="Village">
        <header className="map-toolbar">
          <div>
            <span className="eyebrow">The clearing</span>
            <span className="map-caption">A place for everyone</span>
          </div>
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            aria-pressed={paused}
          >
            {paused ? "Resume motion" : "Pause motion"}
          </button>
        </header>
        <div className="village-canvas">
          <PhaserGame
            snapshot={snapshot}
            selectedId={selectedId}
            onSelectVillager={setSelectedId}
            paused={paused}
          />
        </div>
        <footer className="map-legend">
          <span>
            <i className="state-dot" data-state="working" />
            Working
          </span>
          <span>
            <i className="state-dot" data-state="resting" />
            Resting
          </span>
          <span>
            <i className="state-dot" data-state="knocking" />
            Needs you
          </span>
          <span>
            <i className="state-dot" data-state="stale" />
            Stale
          </span>
          <span className="map-hint">Select a villager to look closer</span>
        </footer>
      </section>
      <aside className="villager-sidebar" aria-label="Villagers">
        <header>
          <p className="eyebrow">Around the village</p>
          <h2>
            People <span>{snapshot.villagers.length}</span>
          </h2>
        </header>
        <label className="search-field">
          <span className="sr-only">Find a villager</span>
          <input
            type="search"
            placeholder="Find a villager…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="people-filters" aria-label="Filter villagers">
          {[
            ["all", "Everyone"],
            ["residents", "Residents"],
            ["working", "Working"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="people-list">
          {villagers.map((v) => (
            <button
              className="person"
              key={v.id}
              type="button"
              aria-pressed={v.id === selectedId}
              onClick={() => setSelectedId(v.id === selectedId ? null : v.id)}
            >
              <Portrait villager={v} />
              <span className="person-copy">
                <strong>
                  {v.name}
                  <small>
                    {v.residency === "resident" ? "Resident" : "Visitor"}
                  </small>
                </strong>
                <span>{v.project || "No project"}</span>
              </span>
              <span className="person-state">
                <i className="state-dot" data-state={v.state} />
                <span>{v.state}</span>
              </span>
            </button>
          ))}
          {!villagers.length && (
            <p className="people-empty">
              {snapshot.villagers.length
                ? "No villagers match this view."
                : "The village is quiet. Residents will appear here when Chronicle observes them."}
            </p>
          )}
        </div>
        {selected ? (
          <section className="person-detail" aria-label="Selected villager">
            <button
              className="close-detail"
              type="button"
              aria-label="Close villager details"
              onClick={() => setSelectedId(null)}
            >
              ×
            </button>
            <p className="eyebrow">
              {selected.residency === "resident"
                ? `Home ${selected.home + 1}`
                : "Staying at the Lodge"}
            </p>
            <h3>{selected.name}</h3>
            <p>{selected.last_line || "No recent activity recorded."}</p>
            <dl>
              <dt>State</dt>
              <dd>{selected.state}</dd>
              <dt>Project</dt>
              <dd>{selected.project || "None"}</dd>
              <dt>Last seen</dt>
              <dd>
                {selected.last_ts ? (
                  <time dateTime={selected.last_ts}>
                    {new Date(selected.last_ts).toLocaleString()}
                  </time>
                ) : (
                  "Unknown"
                )}
              </dd>
            </dl>
          </section>
        ) : (
          <p className="sidebar-note">
            Residents have a home here.
            <br />
            Visitors gather at the Lodge.
          </p>
        )}
      </aside>
    </div>
  );
}
