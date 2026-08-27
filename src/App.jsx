import fixture from "./contract/fixtures/complete-v1.json";
import { parseSnapshot } from "./contract/parseSnapshot.js";
import { PhaserGame } from "./game/PhaserGame.jsx";
import { ReadOnlyPanels } from "./panels/ReadOnlyPanels.jsx";

export function App({ envelope = fixture }) {
  if (envelope === null) {
    return (
      <main className="app-shell app-shell--loading">
        <p role="status">Village snapshot has not loaded yet.</p>
      </main>
    );
  }

  let snapshot;

  try {
    snapshot = parseSnapshot(envelope);
  } catch (error) {
    return (
      <main className="app-shell app-shell--error">
        <section className="contract-error" role="alert">
          <p className="eyebrow">Contract mismatch</p>
          <h1>Arcadia cannot enter this village.</h1>
          <p>{error instanceof Error ? error.message : "Invalid village snapshot"}</p>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="masthead">
        <div>
          <p className="eyebrow">Burrow · generation {snapshot.generation}</p>
          <h1>Arcadia</h1>
        </div>
        <p className="status">{snapshot.villagers.length} villager online</p>
      </header>

      <section className="village" aria-label="Village">
        <PhaserGame snapshot={snapshot} />
        <div className="villagers" aria-label="Villagers">
          {snapshot.villagers.map((villager) => (
            <article className="villager" key={villager.id}>
              <span
                className="villager__mark"
                style={{ "--villager-accent": villager.accent }}
                aria-hidden="true"
              />
              <div>
                <h2>{villager.name}</h2>
                <p>{villager.project || "Wandering"}</p>
              </div>
              <span className="villager__state">{villager.state}</span>
            </article>
          ))}
        </div>
      </section>
      <ReadOnlyPanels snapshot={snapshot} />
    </main>
  );
}
